from __future__ import annotations
import abc
import typing
from functools import partial
from typing import List

import torch
import torch.nn.functional as F
import wandb
from fast_pytorch_kmeans import KMeans
from torch.distributions import Categorical
from torch_geometric.data import Data
from torch_geometric.nn import dense_diff_pool, DenseGCNConv
from torch_geometric.utils import add_remaining_self_loops, to_dense_batch, k_hop_subgraph
from torch_scatter import scatter

import custom_logger
import graphutils
import perturbations
from custom_logger import log
from graphutils import adj_to_edge_index
from poolblocks.custom_asap import ASAPooling

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from custom_net import CustomNet

def rgb2hex(r: int, g: int, b: int):
    return f'#{r:02x}{g:02x}{b:02x}'

def rgb2hex_tensor(ten: torch.Tensor):
    ten = torch.round(ten).to(int)
    return rgb2hex(ten[0].item(), ten[1].item(), ten[2].item())
class PoolBlock(torch.nn.Module, abc.ABC):
    def __init__(self, embedding_sizes: List[int], conv_type=DenseGCNConv,
                 activation_function=F.relu, forced_embeddings=None, **kwargs):
        super().__init__()
        self.forced_embeddings = forced_embeddings
        self.activation_function = activation_function
        self.embedding_sizes = embedding_sizes

    @abc.abstractmethod
    def forward(self, x: torch.Tensor, adj_or_edge_index: torch.Tensor, mask_or_batch=None,
                edge_weights=None):
        """
        Either takes x, adj and mask (for dense data) or x, edge_index and batch (for sparse data)
        :param x: [batch, num_nodes_max, num_features] (dense) or [num_nodes_total, num_features] (sparse)
        :param adj_or_edge_index: [batch, num_nodes_max, num_nodes_max] or [2, num_edges_total]
        :param mask_or_batch: [batch, num_nodes_max] or [num_nodes_total]
        :param edge_weights:
        :return:
        - new_embeddings [batch, num_nodes_new, num_features_new] or [num_nodes_new_total, num_features_new]
        - new_adj_or_edge_index
        - new_edge_weights (for sparse pooling methods were necessary)
        - pool_loss
        - pool: assignment
        - old_embeddings: Embeddings that went into the pooling operation
        - batch_or_mask
        """
        pass

    def log_assignments(self, model: CustomNet, data: Data, num_graphs_to_log: int, epoch: int):
        pass

    def end_epoch(self):
        pass

    @property
    def input_dim(self):
        return self.embedding_sizes[0]

    @property
    def output_dim(self):
        return self.embedding_sizes[-1]

class DenseNoPoolBlock(PoolBlock):
    """
    Dense layers without pooling
    """

    def __init__(self, embedding_sizes: List[int], conv_type=DenseGCNConv,
                 activation_function=F.relu, forced_embeddings=None, **kwargs):
        super().__init__(embedding_sizes, conv_type, activation_function, forced_embeddings, **kwargs)
        self.embedding_convs = torch.nn.ModuleList([conv_type(embedding_sizes[i], embedding_sizes[i + 1])
                                                    for i in range(len(embedding_sizes) - 1)])

    def forward(self, x: torch.Tensor, adj: torch.Tensor, mask=None, edge_weights=None):
        if self.forced_embeddings is not None:
            x = torch.ones(x.shape[:-1] + (self.output_dim,), device=x.device) * self.forced_embeddings
        else:
            for conv in self.embedding_convs:
                x = self.activation_function(conv(x, adj, mask))
        return x, adj, None, 0, None, x, mask

class DiffPoolBlock(PoolBlock):
    def __init__(self, embedding_sizes: List[int], conv_type=DenseGCNConv,
                 activation_function=F.relu, forced_embeddings=None, **kwargs):
        """

        :param sizes: [input_size, hidden_size1, hidden_size2, ..., output_size]
        """
        super().__init__(embedding_sizes, conv_type, activation_function, forced_embeddings)
        # Sizes of layers for generating the pooling embedding could be chosen completely arbitrary.
        # Sharing the first layers and only using a different one for the last layer would be imaginable, too.
        pool_sizes = embedding_sizes.copy()
        pool_sizes[-1] = kwargs["num_output_nodes"]

        self.embedding_convs = torch.nn.ModuleList()
        self.pool_convs = torch.nn.ModuleList()
        for i in range(len(embedding_sizes) - 1):
            # Using DenseGCNConv so I can use adjacency matrix instead of edge_index and don't have to convert back and forth for DiffPool https://github.com/pyg-team/pytorch_geometric/issues/881
            self.embedding_convs.append(conv_type(embedding_sizes[i], embedding_sizes[i + 1]))
            self.pool_convs.append(conv_type(pool_sizes[i], pool_sizes[i + 1]))

        self.cluster_colors = torch.tensor([[1., 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 1], [0, 0, 0]])[None, :, :]

    def forward(self, x: torch.Tensor, adj: torch.Tensor, mask=None, edge_weights=None):
        """
        :param x:
        :param edge_index:
        :return:
        """
        embedding, pool = x, x
        if self.forced_embeddings is None:
            for conv in self.embedding_convs:
                # print("i", embedding.shape, adj.shape)
                embedding = self.activation_function(conv(embedding, adj, mask))
                # embedding = F.dropout(embedding, training=self.training)

            # Don't need the softmax part from http://arxiv.org/abs/2207.13586 as my concepts are determined by the clusters
            # we map to. These could be found using embeddings (separately learned as here in DiffPool or just the normal
            # ones) but don't have to (like in DiffPool)
            # embedding = F.softmax(self.embedding_convs[-1](embedding, adj), dim=1)
            # max_vals, _ = torch.max(embedding, dim=1, keepdim=True)
            # embedding = embedding / max_vals
        else:
            # [batch_size, num_nodes, 1] that is 1 iff node has any neighbours
            # TODO:
            #   1. Note this also means I overwrite graphs with a single node with 0 instead of one. I suppose, I should
            #      rather use the mask
            #   2. Change feature dimension to size of final layer to make this compatible with poolblocks that change
            #      dimension (which should be basically all, wtf)
            embedding = self.forced_embeddings * torch.max(adj, dim=1)[0][:, :, None]

        for conv in self.pool_convs[:-1]:
            pool = self.activation_function(conv(pool, adj))
            # pool = F.dropout(pool, training=self.training)
        pool = self.pool_convs[-1](pool, adj)

        # TODO try dividing the softmax by its maximum value similar to the concepts
        # print(embedding.shape, edge_index.shape, pool.shape) [batch_nodes, num_features] [2, ?] []
        new_embeddings, new_adj, loss_l, loss_e = dense_diff_pool(embedding, adj, pool)
        # DiffPool will result in the same number of output nodes for all graphs, so we don't need to mask any nodes in
        # subsequent layers
        mask = None
        return new_embeddings, new_adj, None, loss_l + loss_e, pool, embedding, mask

    def log_assignments(self, model: CustomNet, data: Data, num_graphs_to_log: int, epoch: int):
        node_table = wandb.Table(["graph", "pool_step", "node_index", "r", "g", "b", "label", "activations"])
        edge_table = wandb.Table(["graph", "pool_step", "source", "target"])
        device = self.embedding_convs[0].bias.device
        with torch.no_grad():
            data = data.clone().detach().to(device)
            out, concepts, _, pool_assignments, pool_activations, _, _, _ = model(data, collect_info=True)
            for graph_i in range(num_graphs_to_log):

                for pool_step, assignment in enumerate(pool_assignments[:1]):
                    num_nodes = data.num_nodes[graph_i]
                    # [num_nodes, num_clusters]
                    assignment = torch.softmax(assignment[graph_i], dim=-1)  # usually performed by diffpool function
                    assignment = assignment.detach().cpu().squeeze(0)  # remove batch dimensions

                    if self.cluster_colors.shape[1] < assignment.shape[1]:
                        raise ValueError(
                            f"Only {self.cluster_colors.shape[1]} colors given to distinguish {assignment.shape[1]} cluster")

                    # [num_nodes, 3] (intermediate dimensions: num_nodes x num_clusters x 3)
                    colors = torch.sum(assignment[:, :, None] * self.cluster_colors[:, :assignment.shape[1], :], dim=1)[
                             :num_nodes, :]
                    colors = torch.round(colors * 255).to(int)
                    for i in range(num_nodes):
                        node_table.add_data(graph_i, pool_step, i, colors[i, 0].item(),
                                            colors[i, 1].item(), colors[i, 2].item(),
                                            ", ".join([f"{m.item() * 100:.0f}%" for m in assignment[i].cpu()]),
                                            ", ".join([f"{m.item():.2f}" for m in
                                                       pool_activations[pool_step][graph_i, i, :].cpu()]))

                    # [3, num_edges] where the first row seems to be constant 0, indicating the graph membership
                    edge_index, _, _ = adj_to_edge_index(data.adj[graph_i], data.mask[graph_i:graph_i+1] if hasattr(data, "mask") else None)
                    for i in range(edge_index.shape[1]):
                        edge_table.add_data(graph_i, pool_step, edge_index[0, i].item(), edge_index[1, i].item())
        log(dict(
            # graphs_table=graphs_table
            node_table=node_table,
            edge_table=edge_table
        ), step=epoch)


class ASAPBlock(PoolBlock):
    """
    After: https://github.com/pyg-team/pytorch_geometric/blob/master/benchmark/kernel/asap.py
    """

    def __init__(self, embedding_sizes: List[int], conv_type=DenseGCNConv,
                 activation_function=F.relu, forced_embeddings=None, num_output_nodes: int = None,
                 ratio_output_nodes: float = None, **kwargs):
        super().__init__(embedding_sizes, conv_type, activation_function, forced_embeddings)
        if num_output_nodes is not None:
            if ratio_output_nodes is not None:
                raise ValueError("Only a fixed number of output nodes (num_output_nodes) or a percentage of input nodes"
                                 "(ratio_output_nodes) can be defined for ASAPPooling but not both.")
            k = num_output_nodes
            assert (isinstance(num_output_nodes, int))
        else:
            k = ratio_output_nodes
            assert (isinstance(k, float))

        self.embedding_convs = torch.nn.ModuleList()
        for i in range(len(embedding_sizes) - 1):
            self.embedding_convs.append(conv_type(embedding_sizes[i], embedding_sizes[i + 1]))
        self.num_output_features = embedding_sizes[-1]

        self.asap = ASAPooling(self.num_output_features, k)
        self.cluster_colors = torch.tensor([
            [22, 160, 133],
            [243, 156, 18],
            [142, 68, 173],
            [39, 174, 96],
            [192, 57, 43],
            [44, 62, 80],
            [41, 128, 185],
            [39, 174, 96],
            [211, 84, 0],
            [121, 85, 72]], dtype=torch.float)

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor, batch=None, edge_weights=None):

        if self.forced_embeddings is None:
            for conv in self.embedding_convs:
                x = self.activation_function(conv(x, edge_index, edge_weight=edge_weights))
        else:
            x = torch.ones(x.shape[:-1] + (self.num_output_features,), device=x.device) * self.forced_embeddings
        new_x, edge_index, new_edge_weight, batch, perm, fitness, score = self.asap(x=x, edge_index=edge_index,
                                                                                    batch=batch,
                                                                                    edge_weight=edge_weights)
        return new_x, edge_index, new_edge_weight, 0, (perm, fitness, score), x, batch

    def log_assignments(self, model: CustomNet, graphs: typing.List[Data], epoch: int):
        node_table = wandb.Table(["graph", "pool_step", "node_index", "r", "g", "b", "border_strength", "border_color",
                                  "label", "activations"])
        edge_table = wandb.Table(["graph", "pool_step", "source", "target", "strength"])  # , "label", "strength"
        device = self.embedding_convs[0].bias.device
        with torch.no_grad():
            for graph_i, data in enumerate(graphs):
                edge_index = data.edge_index.detach().clone()
                edge_index, _ = add_remaining_self_loops(edge_index, fill_value=1., num_nodes=data.num_nodes)

                data = data.clone().detach().to(device)
                num_nodes = data.num_nodes  # note that this will be changed to tensor in model call
                out, concepts, _, assigment_info, pool_activations = model(data)

                for pool_step, (perm, fitness, score) in enumerate(assigment_info[:1]):
                    # [num_nodes]
                    perm = perm.detach().cpu()
                    fitness = fitness.detach().cpu()
                    score = score.detach().cpu()

                    # [num_nodes, 3] (intermediate dimensions: num_nodes x num_clusters x 3)
                    colors = torch.zeros((num_nodes, 3))
                    colors[perm, :] = self.cluster_colors[:perm.shape[0]]

                    # perform inverse thing from ASAP (color the nodes that effected the outcome)
                    # Crucially, colors is 0 for all nodes that are not in perm, so their values are not gonna effect the result
                    color_to_edge = colors[edge_index[1]] * score.view(-1, 1)
                    colors = scatter(color_to_edge, edge_index[0], dim=0, reduce='sum')
                    colors[perm, :] = self.cluster_colors[:perm.shape[
                        0]]  # Make sure the selected nodes actually have their color (with full opacity)
                    for i in range(num_nodes):
                        node_table.add_data(graph_i, pool_step, i, colors[i, 0].item(),
                                            colors[i, 1].item(), colors[i, 2].item(),
                                            2 * fitness[i].item(),
                                            "#F00" if i in perm else "#000",
                                            f"fitness: {fitness[i].item(): .3f}",
                                            ", ".join([f"{m.item():.2f}" for m in
                                                       pool_activations[pool_step][i, :].cpu()]))

                    # [3, num_edges] where the first row seems to be constant 0, indicating the graph membership
                    for i in range(edge_index.shape[1]):
                        edge_table.add_data(graph_i, pool_step, edge_index[0, i].item(), edge_index[1, i].item(),
                                            2 * score[i].item())
        log(dict(
            # graphs_table=graphs_table
            node_table=node_table,
            edge_table=edge_table
        ), step=epoch)


def _calculate_local_clusters(concepts: torch.Tensor, adj: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    """
    :param concepts: [batch_size, max_num_nodes] integer array with values in {0, ..., num_concepts - 1}
    :param adj: [batch_size, max_num_nodes ]
    :param mask: [batch_size, max_num_nodes]
    :return: [batch_size, max_num_nodes] integer array with values in {0, ..., max_num_clusters} that maps all
        connected nodes of the same color to one cluster. Crucially, value 0 is reserved for masked nodes and should be
        removed after scatter.
    """
    batch_size = adj.shape[0]
    num_nodes = adj.shape[1]
    clusters = torch.zeros_like(concepts, dtype=torch.long)

    def rec_color(b: int, i: int):
        clusters[b, i] = cur_color
        for j in range(num_nodes):
            # for now we assume float values in {0, 1} and use 0.5 as threshold to
            # avoid numerical instabilities when doing e.g. == 0
            if mask[b, j] == 0 and clusters[b, j] == 0 and adj[b, i, j] > 0.5 and concepts[b, i] == concepts[b, j]:
                rec_color(b, j)

    for b in range(batch_size):
        cur_color = 1
        for i in range(num_nodes):
            if mask[b,i] and clusters[b, i] == 0:
                rec_color(b, i)
                cur_color += 1

    return clusters

def _calculate_local_clusters_scipy(concepts: torch.Tensor, adj: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    """
    :param concepts: [batch_size, max_num_nodes] integer array with values in {0, ..., num_concepts - 1}
    :param adj: [batch_size, max_num_nodes, max_num_nodes]
    :param mask: [batch_size, max_num_nodes]
    :return: [batch_size, max_num_nodes] integer array with values in {0, ..., max_num_clusters} that maps all
        connected nodes of the same color to one cluster. Crucially, value 0 is reserved for masked nodes and should be
        removed after scatter.
    """
    # [batch_size, max_num_nodes, max_num_nodes]: masking all edges between nodes of different color
    adj = torch.where(concepts[:, :, None] == concepts[:, None, :], adj, 0)
    _, assignments = graphutils.dense_components(adj, mask)
    return assignments


def _perturbed_clustering(x: torch.Tensor, adj: torch.Tensor,
                          cluster_membership_fun: typing.Callable[[torch.Tensor], torch.Tensor],
                          mask: torch.Tensor) -> typing.Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    TODO at least use concept predictions instead of x as the thing we differentiate w.r.t.
    TODO does it make sense to apply things like softmax anyway to get closer to the values we round to before the
     non-differentiable operations?
    TODO use mask?
    TODO: this is NOT theoretically sound yet
    TODO: Even if I plug this in here, I will never get gradients with respect to edge index.
    -> This is fine for the first pooling step as I can't change the initial graph/edges anyway
        -> although that would be an interesting approach for graph rewiring
    -> In the future, maybe I could use a float adjacency matrix and enable require_grad to allow not only the
        node values at layer k-1 but also the graph structure at layer k-1 to be influenced by the outcome of layer k
        - For this, I might be able to maintain a float adjacency matrix and use Gumble softmax to discretize it in
          the actual steps (Caution: That kinda means we're using Gumble softmax in some generalization of Gumble
          softmax)

    :param x: [batch_size, max_num_nodes, hidden_dim]
    :param adj: [batch_size, max_num_nodes, max_num_nodes]
    :param cluster_membership_fun: [batch_size, max_num_nodes, hidden_dim] -> [batch_size, max_num_nodes, num_concepts]
    :param mask: [batch_size, max_num_nodes]???
    :return:
        x_new: [batch_size, max_num_clusters, hidden_dim]
        adj_new: [batch_size, max_num_clusters, max_num_clusters]
        mask_new: [batch_size, max_num_clusters]
        assignments: [batch_size, max_num_nodes] maps nodes to (integer cluster ids). Special cluster 0 reserved for masked nodes
    """
    # [batch_size, max_num_nodes, num_concepts]
    cluster_memberships = cluster_membership_fun(x)
    concept_assignments = torch.argmax(cluster_memberships, dim=-1)
    # [batch_size, max_num_nodes]
    # assignments = _calculate_local_clusters(concept_assignments, adj, mask)
    assignments = _calculate_local_clusters_scipy(concept_assignments, adj, mask)

    # [batch_size, max_num_clusters, hidden_dim] summed representations of the nodes in each cluster with index 0 (masked nodes) removed
    x_new = scatter(x, assignments[:, :, None], reduce="sum", dim=-2)[:, 1:, :]
    # [batch_size, max_num_nodes, max_num_clusters]: for each node: all clusters it points to (with index 0 (masked nodes) removed)
    adj_new = scatter(adj, assignments[:, :, None], reduce="max", dim=-2)[:, 1:, :]
    # [batch_size, max_num_clusters, max_num_clusters]: for each cluster: all clusters it points to  (with index 0 (masked nodes) removed)
    adj_new = scatter(adj_new, assignments[:, None, :], reduce="max", dim=-1)[:, :, 1:]

    # [batch_size] Note that this gives the number of clusters, not the index because 0 is the placeholder for masked nodes
    num_clusters, _ = torch.max(assignments, dim=-1)
    # [batch_size, max_num_clusters]: True iff cluster/new node index is valid / less than the number of clusters in that batch element
    mask_new = torch.arange(x_new.shape[-2], device=custom_logger.device)[None, :] < num_clusters[:, None]
    return x_new, adj_new, mask_new, assignments


class PerturbedBlock(PoolBlock):
    """
    TODO: Main takeaway: Gradient Estimation via Monte Carlo Sampling might be a good idea. Main challenge:
        - Is there any way I can "merge" to a single gradient after a layer like they did?
            - Otherwise, the number of examples would grow exponentially in the pooling layers
            - But I think it could still be feasible for moderately sized graphs with 2 optimizations:
                1. Only recompute clustering if the soft input concept assignment change the hard assignment of a node
                    (makes larger numbers of samples feasible)
                2. Maybe there is some way to average over output embeddings and only get one forward pass per different
                    output graph structure. Kinda doubt it though as we can't jus average the inputs in DL to get average
                    gradients



    TODO If this works, it could be expanded with sth like stick-breaking VAE to support a variable number of clusters
    """

    def __init__(self, embedding_sizes: List[int], conv_type=DenseGCNConv,
                 activation_function=F.relu, forced_embeddings=None, num_concepts: int = None,
                 **kwargs):
        super().__init__(embedding_sizes, conv_type, activation_function, forced_embeddings, **kwargs)
        self.embedding_convs = torch.nn.ModuleList([conv_type(embedding_sizes[i], embedding_sizes[i + 1])
                                                    for i in range(len(embedding_sizes) - 1)])
        self.num_output_features = embedding_sizes[-1]

        self.concept_layer = torch.nn.Sequential(
            torch.nn.Linear(embedding_sizes[-1], embedding_sizes[-1]),
            torch.nn.ReLU(),
            torch.nn.Linear(embedding_sizes[-1], num_concepts))

    def forward(self, x: torch.Tensor, adj: torch.Tensor, mask=None, edge_weights=None, num_samples=100):
        if self.forced_embeddings is not None:
            x = torch.ones(x.shape[:-1] + (self.num_output_features,), device=x.device) * self.forced_embeddings
        else:
            for conv in self.embedding_convs:
                x = self.activation_function(conv(x, adj, mask))
        # TODO this is fine for the first layer as the input graph is fixed but does not allow us to back-propagate from
        #  later layers to adjust the structure in previous ones. Maybe Gumbel softmax??
        # TODO check that what they do actually resembles repeat_interleave not repeat. Otherwise, also change averaging
        #  of output accordingly
        adj_all = torch.repeat_interleave(adj, num_samples, dim=0)
        mask_all = torch.repeat_interleave(mask, num_samples, dim=0)
        cluster_fun = partial(_perturbed_clustering, adj=adj_all, cluster_membership_fun=self.concept_layer,
                              mask=mask_all)
        # TODO
        # print("num_samples seems to increase the batch dimension of the input whereas none of the other stuff (adj, ...)"
        #       "get broadcasted. Thought recalculating everything (clustering etc) would be deadly but just realized "
        #       "there's no way around this as the whole differentiability relies on it.")
        cluster_fun = perturbations.perturbed(cluster_fun, device=custom_logger.device, num_samples=num_samples)

        # x_new: [batch_size, max_num_clusters, embedding_size]
        # adj_new: [batch_size * num_samples, max_num_clusters, max_num_clusters]
        # mask_new: [batch_size * num_samples, max_num_clusters]
        x_new, adj_new, mask_new, assignments = cluster_fun(x)
        # TODO those 2 choices can't really work, check code
        adj_new = torch.mean(adj_new.reshape(num_samples, -1, adj_new.shape[-2], adj_new.shape[-1]), dim=0)
        mask_new, _ = torch.max(mask_new.reshape(num_samples, -1, mask_new.shape[-1]), dim=0)
        return x_new, adj_new, None, 0, assignments, x, mask_new


class SingleMCBlock(PoolBlock):
    """
    TODO: Challenge: how do I even generate a probability distribution over clustered graphs
        -> just sample before the discrete operation (edge generation), I guess


    TODO currently a little shady as we use 1-dimensional embeddings which we then cluster. Maybe turn this up
        (from [layer_sizes])
    """
    def __init__(self, embedding_sizes: List[int], conv_type=DenseGCNConv,
                 activation_function=F.relu, forced_embeddings=None, num_concepts: int = None,
                 final_bottleneck: typing.Optional[int] = None, global_clusters: bool = True, soft_sampling: float = 0,
                 clustering_loss_weight: float = 0.0,
                 **kwargs):
        """

        :param embedding_sizes:
        :param conv_type:
        :param activation_function:
        :param forced_embeddings:
        :param num_concepts:
        :param final_bottleneck: if provided, inserts a linear layer AFTER the clustering
        :param global_clusters:
        :param soft_sampling: If 0, points will always be mapped to the nearest cluster. Otherwise, this will be the
        temperature of a softmax that gives the probability a with which a point will be mapped to each cluster, based
        on the distance from the centroid.
        :param kwargs:
        """
        super().__init__(embedding_sizes, conv_type, activation_function, forced_embeddings, **kwargs)
        self.embedding_convs = torch.nn.ModuleList([conv_type(embedding_sizes[i], embedding_sizes[i + 1])
                                                    for i in range(len(embedding_sizes) - 1)])
        self.num_output_features = embedding_sizes[-1]

        # In the current setup, training this layer might be difficult as we don't really get gradients
        # self.concept_layer = torch.nn.Sequential(
        #     torch.nn.Linear(embedding_sizes[-1], embedding_sizes[-1]),
        #     torch.nn.ReLU(),
        #     torch.nn.Linear(embedding_sizes[-1], num_concepts))
        self.kmeans = KMeans(n_clusters=num_concepts, mode='euclidean', verbose=0)
        self.seen_embeddings = torch.empty((0, self.num_output_features), device=custom_logger.device)
        self.global_clusters = global_clusters
        # In the first epoch, we haven't seen the data yet, so we always need to use local clusters. After that,
        # use_global_clusters will be equal to global_clusters
        self.use_global_clusters = False
        self.soft_sampling = soft_sampling
        self.num_concepts = num_concepts
        self.final_bottleneck_dim = final_bottleneck
        self.clustering_loss_weight = clustering_loss_weight
        if self.final_bottleneck_dim:
            self.final_bottleneck = torch.nn.Linear(embedding_sizes[-1], final_bottleneck)
        else:
            self.final_bottleneck = None
        self.cluster_colors = torch.tensor([
            [22, 160, 133],
            [243, 156, 18],
            [142, 68, 173],
            [192, 57, 43],
            [44, 62, 80],
            [41, 128, 185],
            [39, 174, 96],
            [211, 84, 0],
            [39, 174, 96],
            [121, 85, 72]], dtype=torch.float)

    def forward(self, x: torch.Tensor, adj: torch.Tensor, mask=None, edge_weights=None):
        if self.forced_embeddings is not None:
            x = torch.ones(x.shape[:-1] + (self.num_output_features,), device=x.device) * self.forced_embeddings
        else:
            for conv in self.embedding_convs:
                x = self.activation_function(conv(x, adj, mask))
        batch_size = x.shape[0]
        if self.global_clusters and self.training:
            # Only adding training data makes it impossible to generalize to new concepts during test but that's kinda
            # unlikely anyway. Alternatively one could append test embeddings but then one would need to undo those
            # changes before starting the next round of training to avoid the test data influencing the training
            self.seen_embeddings = torch.cat((self.seen_embeddings, x[mask].detach()), dim=0)
        if not self.use_global_clusters:
            self.kmeans.fit(x[mask])

        if (self.soft_sampling != 0 and self.training) or self.clustering_loss_weight != 0:
            # https://ai.stackexchange.com/questions/13776/how-is-reinforce-used-instead-of-backpropagation
            # [num_nodes, num_concepts] (centroids: [num_concepts, embedding_size])
            distances = torch.cdist(x[mask], self.kmeans.centroids)

        if self.soft_sampling == 0 or not self.training:
            concept_assignments = self.kmeans.predict(x[mask])
        else:
            assignment_probs = torch.nn.functional.softmin(distances / self.soft_sampling, dim=-1)
            distr = Categorical(assignment_probs)
            concept_assignments = distr.sample()
        # [batch, max_num_nodes] take only the embeddings of nodes that are not masked (otherwise we would
        # get different clusters). Apply kmeans and convert back to dense representation
        concept_assignments, mask_temp = to_dense_batch(concept_assignments,
                                                        batch=graphutils.batch_from_mask(mask, x.shape[1]),
                                                        batch_size=batch_size, max_num_nodes=x.shape[1])
        assert torch.all(mask_temp == mask)
        # [batch_size, max_num_nodes] assigns each node to a cluster. 0 for masked nodes
        assignments = _calculate_local_clusters_scipy(concept_assignments, adj, mask)

        x_new = scatter(x, assignments[:, :, None], reduce="sum", dim=-2)[:, 1:, :]
        if self.final_bottleneck is not None:
            # Because both transformations are linear, this should be equivalent to applying it before pooling
            x_new = self.final_bottleneck(x_new)
        # [batch_size, max_num_nodes, max_num_clusters]: for each node: all clusters it points to (with index 0 (masked nodes) removed)
        adj_new = scatter(adj, assignments[:, :, None], reduce="max", dim=-2)[:, 1:, :]
        # [batch_size, max_num_clusters, max_num_clusters]: for each cluster: all clusters it points to  (with index 0 (masked nodes) removed)
        adj_new = scatter(adj_new, assignments[:, None, :], reduce="max", dim=-1)[:, :, 1:]

        # [batch_size] Note that this gives the number of clusters, not the index because 0 is the placeholder for masked nodes
        num_clusters, _ = torch.max(assignments, dim=-1)
        # [batch_size, max_num_clusters]: True iff cluster/new node index is valid / less than the number of clusters in that batch element
        mask_new = torch.arange(x_new.shape[-2], device=custom_logger.device)[None, :] < num_clusters[:, None]
        if self.clustering_loss_weight == 0:
            clustering_loss = 0
        else:
            clustering_loss = self.clustering_loss_weight * torch.linalg.vector_norm(torch.min(distances, dim=-1)[0])
            if clustering_loss >= 10:
                # Cap clustering loss at 10 to avoid numerical instability
                clustering_loss /= (clustering_loss.detach() / 10)
        return x_new, adj_new, None, clustering_loss, concept_assignments, x, mask_new

    def log_assignments(self, model: CustomNet, data: Data, num_graphs_to_log: int, epoch: int):
        # TODO adjust visualizations for other graphs to new signature
        # IMPORTANT: Here it is crucial to have batches of the size used during training in the forward pass
        # if using only a single example, some concepts might not be present but we still enforce the same number of
        # clusters
        TEMPERATURE = 0.1  # softmax temperature that makes clearer which node is assigned to which cluster
        node_table = wandb.Table(["graph", "pool_step", "node_index", "r", "g", "b", "border_color", "label", "activations"])
        edge_table = wandb.Table(["graph", "pool_step", "source", "target"])
        device = custom_logger.device
        with torch.no_grad():
            data = data.clone().detach().to(device)
            # concepts: [batch_size, max_num_nodes_final_layer, embedding_dim_out_final_layer] the node embeddings of the final graph
            # pool_assignments: []
            out, concepts, _, pool_assignments, pool_activations, adjs, masks, input_embeddings =\
                model(data, collect_info=True)
            pool_assignments = [pool_assignments[i] for i in range(len(pool_assignments))
                                if not isinstance(model.graph_network.pool_blocks[i], DenseNoPoolBlock)]
            masks = [data.mask] + masks
            adjs = [data.adj] + adjs
            pool_activations = [data.x] + pool_activations
            input_embeddings = [data.x] + input_embeddings
            centroids = [torch.eye(data.x.shape[-1], device=custom_logger.device)] + \
                        [pb.kmeans.centroids if hasattr(pb, "kmeans") else None for pb in model.graph_network.pool_blocks]

            ############################## Print Probability Distributions #########
            for pool_step in range(len(pool_assignments)):
                temperature = getattr(model.graph_network.pool_blocks[pool_step], "soft_sampling", 0)
                if temperature != 0:
                    distances = torch.cdist(pool_activations[pool_step + 1][masks[pool_step]],
                                            centroids[pool_step + 1])
                    max_probs, arg_max = torch.max(torch.nn.functional.softmin(distances / temperature, dim=-1), dim=-1)
                    hard_assignments = model.graph_network.pool_blocks[pool_step].kmeans.\
                        predict(pool_activations[pool_step + 1][masks[pool_step]])
                    print(f"\nProbability of most likely concept in pooling step {pool_step}: "
                          f"{100*torch.mean(max_probs):.2f}%+-{100*torch.std(max_probs):.2f} with "
                          f"{100 * torch.sum(hard_assignments == arg_max) / arg_max.shape[0]:.2f}% of the soft maxima "
                          f"agreeing with the hard assignment")
            ############################## Log Graphs ##############################
            for graph_i in range(num_graphs_to_log):
                for pool_step, assignment in enumerate(pool_assignments):
                    # Calculate concept assignment colors
                    # [num_nodes] (with batch dimension and masked nodes removed)
                    assignment = assignment[graph_i][masks[pool_step][graph_i]].detach().cpu()
                    if self.cluster_colors.shape[0] <= torch.max(assignment):
                        raise ValueError(
                            f"Only {self.cluster_colors.shape[0]} colors given to distinguish {torch.max(assignment)} "
                            f"clusters!")
                    # [num_nodes, 3] (intermediate dimensions: num_nodes x num_clusters x 3)
                    concept_colors = self.cluster_colors[assignment, :]

                    # Calculate feature colors
                    # [num_nodes_in_neighbourhood, num_concepts] where (i, j) gives difference between node i and concept j
                    feature_colors = torch.cdist(pool_activations[pool_step][graph_i, masks[pool_step][graph_i]],
                                                 centroids[pool_step])
                    if feature_colors.shape[1] > self.cluster_colors.shape[0]:
                        raise ValueError(f"Cannot visualize {feature_colors.shape[1]} using "
                                         f"{self.cluster_colors.shape[0]} colors!")
                    feature_colors = torch.sum(torch.nn.functional.softmin(feature_colors / TEMPERATURE, dim=1)[:, :, None].cpu() *
                                               self.cluster_colors[None, :feature_colors.shape[1], :], dim=1)
                    feature_colors = torch.round(feature_colors).to(int)

                    for i, i_old in enumerate(masks[pool_step][graph_i].nonzero().squeeze(1)):
                        node_table.add_data(graph_i, pool_step, i, feature_colors[i, 0].item(),
                                            feature_colors[i, 1].item(), feature_colors[i, 2].item(),
                                            rgb2hex_tensor(concept_colors[i, :]),
                                            f"Cluster {assignment[i]}",
                                            ", ".join([f"{m.item():.2f}" for m in
                                                       pool_activations[pool_step][graph_i, i_old, :].cpu()]))

                    edge_index, _, _ = adj_to_edge_index(adjs[pool_step][graph_i:graph_i+1, :, :],
                                                         masks[pool_step][graph_i:graph_i+1])
                    for i in range(edge_index.shape[1]):
                        edge_table.add_data(graph_i, pool_step, edge_index[0, i].item(), edge_index[1, i].item())
            log(dict(
                # graphs_table=graphs_table
                node_table=node_table,
                edge_table=edge_table
            ), step=epoch)

            ############################## Log Concept Examples ##############################
            SAMPLES_PER_CONCEPT = 3
            # This is not ideal as we only sample concepts from the same (first) batch. But we could increase it's size

            for concept in range(self.num_concepts):
                node_table = wandb.Table(["graph", "pool_step", "node_index", "r", "g", "b", "border_color", "label",
                                          "activations"])
                edge_table = wandb.Table(["graph", "pool_step", "source", "target"])
                for pool_step, assignment in enumerate(pool_assignments):
                    samples_seen = 0
                    # [num_nodes_with_concept_total, 2] with all pairs of (batch_index, node_index) of nodes that are
                    # not masked and are classified to a certain example
                    example_nodes = (torch.logical_and(masks[pool_step], assignment == concept)).nonzero()
                    for sample, node in example_nodes[torch.randperm(example_nodes.shape[0]), :]:
                        if samples_seen >= SAMPLES_PER_CONCEPT:
                            break
                        edge_index_prev, _, _ = adj_to_edge_index(adjs[pool_step][sample])
                        subset, edge_index, _, _ = k_hop_subgraph(node.item(), len(self.embedding_convs),
                                                                  edge_index_prev,
                                                                  relabel_nodes=True,
                                                                  num_nodes=torch.sum(masks[pool_step][sample]).item())

                        # [num_nodes_in_neighbourhood, num_concepts] where (i, j) gives difference between node i and concept j
                        distances = torch.cdist(pool_activations[pool_step][sample, masks[pool_step][sample]][subset],
                                                centroids[pool_step])
                        if distances.shape[1] > self.cluster_colors.shape[0]:
                            raise ValueError(f"Cannot visualize {distances.shape[1]} using "
                                             f"{self.cluster_colors.shape[0]} colors!")
                        feature_colors = torch.sum(torch.nn.functional.softmin(distances / TEMPERATURE, dim=1)[:, :, None].cpu() *
                                                   self.cluster_colors[None, :distances.shape[1], :], dim=1)
                        feature_colors = torch.round(feature_colors).to(int)

                        for i in range(subset.shape[0]):
                            node_table.add_data(samples_seen, pool_step, i, feature_colors[i, 0].item(),
                                                feature_colors[i, 1].item(), feature_colors[i, 2].item(),
                                                "#F00" if subset[i] == node.item() else "#FFF",
                                                "Distances: " + ", ".join([f"{m.item():.2f}" for m in distances[i, :].cpu()]), "")
                        for i in range(edge_index.shape[1]):
                            edge_table.add_data(samples_seen, pool_step, edge_index[0, i].item(), edge_index[1, i].item())
                        samples_seen += 1
                log({
                    f"concept_node_table_{concept}": node_table,
                    f"concept_edge_table_{concept}": edge_table
                }, step=epoch)




    def end_epoch(self):
        if not self.global_clusters:
            return
        self.use_global_clusters = True
        self.kmeans.fit(self.seen_embeddings)
        self.seen_embeddings = torch.empty((0, self.num_output_features), device=custom_logger.device)

    @PoolBlock.output_dim.getter
    def output_dim(self):
        return self.embedding_sizes[-1] if self.final_bottleneck_dim is None else self.final_bottleneck_dim

__all_dense__ = [DenseNoPoolBlock, DiffPoolBlock, PerturbedBlock, SingleMCBlock]
__all_sparse__ = [ASAPBlock]


def from_name(name: str, dense_data: bool):
    for b in __all_dense__ if dense_data else __all_sparse__:
        if b.__name__ == name + "Block":
            return b
    raise ValueError(f"Unknown pooling type {name} for dense_data={dense_data}!")#

def valid_names() -> List[str]:
    return [b.__name__[:-5] for b in __all_dense__] + [b.__name__[:-5] for b in __all_sparse__]
