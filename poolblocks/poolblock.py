from __future__ import annotations
import abc
import json
import typing
import warnings
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from typing import List

import networkx as nx
import torch
import torch.nn.functional as F
import wandb
from functorch import vmap
from torch.distributions import Categorical
from torch_geometric.data import Data
from torch_geometric.nn import dense_diff_pool, DenseGCNConv
from torch_geometric.utils import add_remaining_self_loops, to_dense_batch, k_hop_subgraph, to_networkx
from torch_scatter import scatter

import clustering_wrappers
import custom_logger
import graphutils
import perturbations
from blackbox_backprop import BlackBoxModule
from blackbox_backprop import BlackBoxModule
from color_utils import ColorUtils
from custom_logger import log
from data_generation import deserializer
from graphutils import adj_to_edge_index
from poolblocks.custom_asap import ASAPooling
import networkx.algorithms.isomorphism as iso

from typing import TYPE_CHECKING

from poolblocks.perturbing_distributions import PerturbingDistribution

if TYPE_CHECKING:
    from custom_net import CustomNet

class PoolBlock(torch.nn.Module, abc.ABC):
    def __init__(self, embedding_sizes: List[int], conv_type=DenseGCNConv,
                 activation_function=F.relu, forced_embeddings=None, directed_graphs: bool = True, **kwargs):
        super().__init__()
        self.forced_embeddings = forced_embeddings
        self.activation_function = activation_function
        self.embedding_sizes = embedding_sizes
        self.directed_graphs = directed_graphs

    @abc.abstractmethod
    def forward(self, x: torch.Tensor, adj_or_edge_index: torch.Tensor, mask_or_batch=None,
                edge_weights=None):
        """
        Either takes x, adj and mask (for dense data) or x, edge_index and batch (for sparse data)
        :param x: [batch, num_nodes_max, num_features] (dense) or [num_nodes_total, num_features] (sparse)
        :param adj_or_edge_index: [batch, num_nodes_max, num_nodes_max] or [2, num_edges_total]
        :param mask_or_batch: [batch, num_nodes_max] or [num_nodes_total]
        :param edge_weights:
        :param
        :return:
        - new_embeddings [batch, num_nodes_new, num_features_new] or [num_nodes_new_total, num_features_new]
        - new_adj_or_edge_index
        - new_edge_weights (for sparse pooling methods were necessary)
        - probabilities: optional [batch_size] vector with probability for each sample that will be used as a
        factor to account for how likely the sampled cluster were
        - pool_loss
        - pool: assignment
        - old_embeddings: Embeddings that went into the pooling operation
        - batch_or_mask
        """
        pass

    def log_data(self, epoch: int, index: int):
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

    @property
    def receptive_field(self):
        return len(self.embedding_sizes) - 1

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
        return x, adj, None, None, 0, None, None, x, mask


class SparseNoPoolBlock(PoolBlock):
    """
    Dense layers without pooling
    """

    def __init__(self, embedding_sizes: List[int], conv_type=DenseGCNConv,
                 activation_function=F.relu, forced_embeddings=None, **kwargs):
        super().__init__(embedding_sizes, conv_type, activation_function, forced_embeddings, **kwargs)
        self.embedding_convs = torch.nn.ModuleList([conv_type(embedding_sizes[i], embedding_sizes[i + 1])
                                                    for i in range(len(embedding_sizes) - 1)])

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor, batch=None, edge_weights=None):
        if self.forced_embeddings is not None:
            x = torch.ones(x.shape[:-1] + (self.output_dim,), device=x.device) * self.forced_embeddings
        else:
            for conv in self.embedding_convs:
                x = self.activation_function(conv(x, edge_index, edge_weights))
        return x, edge_index, None, None, 0, None, None, x, batch


class DiffPoolBlock(PoolBlock):
    def __init__(self, embedding_sizes: List[int], conv_type=DenseGCNConv,
                 activation_function=F.relu, forced_embeddings=None, num_output_nodes: int = -1, **kwargs):
        """

        :param sizes: [input_size, hidden_size1, hidden_size2, ..., output_size]
        """
        super().__init__(embedding_sizes, conv_type, activation_function, forced_embeddings)
        # Sizes of layers for generating the pooling embedding could be chosen completely arbitrary.
        # Sharing the first layers and only using a different one for the last layer would be imaginable, too.
        pool_sizes = embedding_sizes.copy()
        self.num_output_nodes = num_output_nodes
        pool_sizes[-1] = num_output_nodes

        self.embedding_convs = torch.nn.ModuleList()
        self.pool_convs = torch.nn.ModuleList()
        for i in range(len(embedding_sizes) - 1):
            # Using DenseGCNConv so I can use adjacency matrix instead of edge_index and don't have to convert back and forth for DiffPool https://github.com/pyg-team/pytorch_geometric/issues/881
            self.embedding_convs.append(conv_type(embedding_sizes[i], embedding_sizes[i + 1]))
            self.pool_convs.append(conv_type(pool_sizes[i], pool_sizes[i + 1]))

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
        return new_embeddings, new_adj, None, None, loss_l + loss_e, pool, pool, embedding, mask

    def log_assignments(self, model: CustomNet, data: Data, num_graphs_to_log: int, epoch: int):
        node_table = wandb.Table(["graph", "pool_step", "node_index", "r", "g", "b", "label", "activations"])
        edge_table = wandb.Table(["graph", "pool_step", "source", "target"])
        device = self.embedding_convs[0].bias.device
        with torch.no_grad():
            data = data.clone().detach().to(device)
            out, _, concepts, _, info = model(data, collect_info=True)
            pool_assignments = info.pooling_assignments
            pool_activations = info.pooling_activations
            for graph_i in range(num_graphs_to_log):

                for pool_step, assignment in enumerate(pool_assignments[:1]):
                    num_nodes = data.num_nodes[graph_i]
                    # [num_nodes, num_clusters]
                    assignment = torch.softmax(assignment[graph_i], dim=-1)  # usually performed by diffpool function
                    assignment = assignment.detach().cpu().squeeze(0)  # remove batch dimensions
                    ColorUtils.ensure_min_rgb_colors(assignment.shape[1] + 1)
                    # [num_nodes, 3] (intermediate dimensions: num_nodes x num_clusters x 3)
                    colors = torch.sum(assignment[:, :, None] * ColorUtils.rgb_colors[None, :assignment.shape[1], :],
                                       dim=1)[:num_nodes, :]
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

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor, batch=None, edge_weights=None):

        if self.forced_embeddings is None:
            for conv in self.embedding_convs:
                # print(x.shape, edge_index.shape)
                x = self.activation_function(conv(x, edge_index, edge_weight=edge_weights))
        else:
            x = torch.ones(x.shape[:-1] + (self.num_output_features,), device=x.device) * self.forced_embeddings
        new_x, edge_index, new_edge_weight, batch, perm, fitness, score = self.asap(x=x, edge_index=edge_index,
                                                                                    batch=batch,
                                                                                    edge_weight=edge_weights)
        return new_x, edge_index, new_edge_weight, None, 0, (perm, fitness, score), None, x, batch

    def log_assignments(self, model: CustomNet, all_data: Data, num_graphs_to_log: int, epoch: int):
        node_table = wandb.Table(["graph", "pool_step", "node_index", "r", "g", "b", "border_strength", "border_color",
                                  "label", "activations"])
        edge_table = wandb.Table(["graph", "pool_step", "source", "target", "strength"])  # , "label", "strength"
        device = self.embedding_convs[0].bias.device
        with torch.no_grad():
            for graph_i in range(num_graphs_to_log):
                mask = all_data.batch == graph_i
                start_index = torch.arange(mask.shape[0])[mask][0]
                num_nodes = torch.sum(mask)
                data = Data(x=all_data.x[start_index:start_index+num_nodes],
                            edge_index=all_data.edge_index[:,
                                       torch.logical_and(all_data.edge_index[0] >= start_index,
                                                         all_data.edge_index[0] < start_index + num_nodes)] - start_index,
                            num_nodes=num_nodes)
                edge_index = data.edge_index.detach().clone()
                edge_index, _ = add_remaining_self_loops(edge_index, fill_value=1., num_nodes=data.num_nodes)

                data = data.clone().detach().to(device)
                num_nodes = data.num_nodes  # note that this will be changed to tensor in model call
                out, _, concepts, _, info = model(data, collect_info=True)
                assigment_info = info.pooling_assignments
                pool_activations = info.pooling_activations

                for pool_step, (perm, fitness, score) in enumerate(assigment_info[:1]):
                    # [num_nodes]
                    perm = perm.detach().cpu()
                    fitness = fitness.detach().cpu()
                    score = score.detach().cpu()

                    # [num_nodes, 3] (intermediate dimensions: num_nodes x num_clusters x 3)
                    colors = torch.zeros((num_nodes, 3))
                    ColorUtils.ensure_min_rgb_colors(perm.shape[0])
                    colors[perm, :] = ColorUtils.rgb_colors[:perm.shape[0]]

                    # perform inverse thing from ASAP (color the nodes that effected the outcome)
                    # Crucially, colors is 0 for all nodes that are not in perm, so their values are not gonna effect the result
                    color_to_edge = colors[edge_index[1]] * score.view(-1, 1)
                    colors = scatter(color_to_edge, edge_index[0], dim=0, reduce='sum')
                    colors[perm, :] = ColorUtils.rgb_colors[:perm.shape[
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

def _calculate_local_clusters_scipy(concepts: torch.Tensor, adj: torch.Tensor, mask: torch.Tensor, is_directed: bool) -> torch.Tensor:
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
    assignments = graphutils.dense_components(adj, mask, is_directed=is_directed)
    return assignments

def _generate_assignments(x_mask, adj, mask, is_directed, batch_size, max_num_nodes, soft_sampling: float, training: bool,
                          clustering_loss_weight: float, num_mc_samples: 1, use_global_clusters: bool,
                          cluster_alg: clustering_wrappers.ClusterAlgWrapper, parallel: bool, transparency: float):
    # Note: if we are not soft sampling, the samples should not have an impact here and are instead meant for the outer
    # function which calls this one with different perturbations
    num_mc_samples = num_mc_samples if training and (soft_sampling != 0 or transparency == 0) else 1
    # Note that pickeling the cluster alg might not be ideal from an efficiency POV
    if not use_global_clusters:
        # Avoid copies if unnecessary
        if parallel:
            cluster_alg = cluster_alg.fit_copy(x_mask)
        else:
            cluster_alg.fit(x_mask.detach(), train=training)

    if (soft_sampling != 0 and training) or clustering_loss_weight != 0:
        # https://ai.stackexchange.com/questions/13776/how-is-reinforce-used-instead-of-backpropagation
        # [num_nodes_total, num_concepts] (centroids: [num_concepts, embedding_size])
        distances = torch.cdist(x_mask, cluster_alg.centroids.detach())
    else:
        distances = None
    batch = graphutils.batch_from_mask(mask, max_num_nodes)
    if soft_sampling == 0 or not training:
        concept_assignments = cluster_alg.predict(x_mask)
        probabilities = None
    else:
        # [num_nodes_total, num_concepts]
        assignment_probs = torch.nn.functional.softmin(distances / soft_sampling, dim=-1)
        distr = Categorical(assignment_probs)
        # [num_mc_samples, num_nodes_total] -> [num_mc_samples * num_nodes_total]
        concept_assignments = distr.sample((num_mc_samples, )).flatten()
        # [num_nodes_total * num_mc_samples] Note: we only want to use those as weights for the loss but not backpropagate w.r.t. them
        probabilities = assignment_probs[
            torch.arange(assignment_probs.shape[0]).repeat(num_mc_samples),
            concept_assignments].detach()
        # [num_nodes_total]
        batch = batch.repeat(num_mc_samples) +\
                torch.arange(num_mc_samples, device=adj.device).repeat_interleave(assignment_probs.shape[0]) * batch_size
        # [batch_size * num_samples, max_num_nodes, max_num_nodes] just a repeated version of the original adjacency
        adj = adj.repeat(num_mc_samples, 1, 1)
        # [batch_size * num_mc_samples]
        probabilities = scatter(probabilities, batch, reduce="mul")

    # [batch_size * num_mc_samples, max_num_nodes] take only the embeddings of nodes that are not masked (otherwise we would
    # get different clusters).
    concept_assignments, mak_temp = to_dense_batch(concept_assignments, batch=batch,
                                                   batch_size=batch_size * num_mc_samples,
                                                   max_num_nodes=max_num_nodes, fill_value=-1)
    # [batch_size * (num_mc_samples if soft_sampling else 1), max_num_nodes] assigns each node to a cluster. 0 for masked nodes
    assignments = _calculate_local_clusters_scipy(concept_assignments, adj, mask.repeat(num_mc_samples, 1), is_directed)

    return assignments, concept_assignments, distances, probabilities, batch, cluster_alg.centroids.shape[0]

class MonteCarloBlock(PoolBlock):
    """
    TODO: Challenge: how do I even generate a probability distribution over clustered graphs
        -> just sample before the discrete operation (edge generation), I guess
    """
    def __init__(self, embedding_sizes: List[int], conv_type=DenseGCNConv, activation_function=F.relu,
                 forced_embeddings=None, directed_graphs: bool = True, cluster_alg: str = "KMeans",
                 final_bottleneck: typing.Optional[int] = None, global_clusters: bool = True, soft_sampling: float = 0,
                 clustering_loss_weight: float = 0.0, perturbation: typing.Optional[dict] = None,
                 num_mc_samples: int = 1, transparency: float = 1, state_dict: typing.Optional[dict] = None,
                 use_centroids_as_embedding: bool = False, **kwargs):
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
        super().__init__(embedding_sizes=embedding_sizes, conv_type=conv_type, activation_function=activation_function,
                         forced_embeddings=forced_embeddings, directed_graphs=directed_graphs, **kwargs)
        self.embedding_convs = torch.nn.ModuleList([conv_type(embedding_sizes[i], embedding_sizes[i + 1])
                                                    for i in range(len(embedding_sizes) - 1)])
        self.num_output_features = embedding_sizes[-1]

        # In the current setup, training this layer might be difficult as we don't really get gradients
        # self.concept_layer = torch.nn.Sequential(
        #     torch.nn.Linear(embedding_sizes[-1], embedding_sizes[-1]),
        #     torch.nn.ReLU(),
        #     torch.nn.Linear(embedding_sizes[-1], num_concepts))
        if state_dict is None:
            state_dict = {}
        self.cluster_alg = clustering_wrappers.get_from_name(cluster_alg)(**kwargs, **state_dict)

        self.seen_embeddings = torch.empty((0, self.num_output_features), device=custom_logger.device)
        self.global_clusters = global_clusters
        # In the first epoch, we haven't seen the data yet, so we always need to use local clusters. After that,
        # use_global_clusters will be equal to global_clusters
        self.use_global_clusters = False
        self.soft_sampling = soft_sampling
        self.final_bottleneck_dim = final_bottleneck
        self.clustering_loss_weight = clustering_loss_weight
        self.perturbation = None if perturbation is None else\
            typing.cast(PerturbingDistribution, deserializer.from_dict(perturbation))
        self.transparency = transparency
        self.last_num_clusters = None
        self.use_centroids_as_embedding = use_centroids_as_embedding
        if use_centroids_as_embedding and\
                (self.num_mc_samples == 1 or self.perturbation is None or self.transparency != 1) and\
                custom_logger.cpu_workers != 0:
            raise NotImplementedError("Centroids as embeddings are not implemented for parallel workers!")

        self.num_mc_samples = num_mc_samples
        if num_mc_samples > 1 and soft_sampling == 0 and perturbation is None:
            raise ValueError(f"Multiple monte carlo samples ({num_mc_samples} given) only make sense when sampling is "
                             f"not deterministic (soft_sampling != 0 or perturbation != None)!")
        if transparency != 1 and perturbation is None:
            raise ValueError(f"A perturbation distribution must be given for hyperplane gradient estimation!")
        if [soft_sampling != 0, perturbation is not None].count(True) > 1:
            raise ValueError("Only one gradient approximation method (like hyperplane approximation, perturbed inputs "
                             "and soft cluster assignments) can be used at once!")
        if self.final_bottleneck_dim:
            self.final_bottleneck = torch.nn.Linear(embedding_sizes[-1], final_bottleneck)
        else:
            self.final_bottleneck = None
        if self.perturbation is not None and self.num_mc_samples != 1 and self.transparency != 1 and custom_logger.cpu_workers > 0:
            self.pool = ProcessPoolExecutor(max_workers=custom_logger.cpu_workers)

    def preprocess(self, x: torch.Tensor, adj: torch.Tensor, mask=None, edge_weights=None):
        if self.forced_embeddings is not None:
            x = torch.ones(x.shape[:-1] + (self.num_output_features,), device=x.device) * self.forced_embeddings
        else:
            for conv in self.embedding_convs:
                x = self.activation_function(conv(x, adj, mask))
        if self.global_clusters and self.training:
            # Only adding training data makes it impossible to generalize to new concepts during test but that's kinda
            # unlikely anyway. Alternatively one could append test embeddings but then one would need to undo those
            # changes before starting the next round of training to avoid the test data influencing the training
            self.seen_embeddings = torch.cat((self.seen_embeddings, x[mask].detach()), dim=0)
        return x

    def perturb(self, x: torch.Tensor):
        return self.perturbation(x) if self.train else x

    def hard_fn(self, x: torch.Tensor, adj: torch.Tensor, mask=None, edge_weights=None):
        num_mc_samples = self.num_mc_samples if self.training and self.transparency == 1 else 1
        generate_assignments = partial(_generate_assignments, adj=adj, mask=mask, is_directed=self.directed_graphs,
                                       batch_size=x.shape[0], max_num_nodes=x.shape[1], soft_sampling=self.soft_sampling,
                                       training=self.training, clustering_loss_weight=self.clustering_loss_weight,
                                       num_mc_samples=self.num_mc_samples, use_global_clusters=self.use_global_clusters,
                                       cluster_alg=self.cluster_alg, transparency=self.transparency)
        batch_size, max_num_nodes = x.shape[:2]

        if self.num_mc_samples == 1 or self.perturbation is None or self.transparency != 1:
            # TODO I don't scale up the mask for the diff calls
            assignments, concept_assignments, distances, probabilities, batch, self.last_num_clusters =\
                generate_assignments(x[mask].detach(), parallel=False)
        else:
            distances = probabilities = None  # We are using perturbation, so definitely no soft sampling
            assignments = torch.empty(batch_size * num_mc_samples, max_num_nodes, device=x.device, dtype=torch.long)
            concept_assignments = torch.empty(batch_size * num_mc_samples, max_num_nodes, device=x.device, dtype=torch.long)
            # [num_nodes_total (for all samples together)]
            batch = torch.empty((0, ), device=x.device, dtype=torch.long)
            if custom_logger.cpu_workers == 0:
                for sample in range(num_mc_samples):
                    # Note that adj is only modified for soft sampling. batch_s is of size [batch_size]
                    ass_s, conc_ass_s, dist_s, prob_s, batch_s, self.last_num_clusters =\
                        generate_assignments(self.perturb(x[mask]).detach(), parallel=False)
                    assignments[sample * batch_size:(sample + 1) * batch_size] = ass_s
                    concept_assignments[sample * batch_size:(sample + 1) * batch_size] = conc_ass_s
                    batch = torch.cat((batch, batch_s), dim=0)
            else:
                generate_assignments = partial(generate_assignments, parallel=True)
                try:
                    res = self.pool.map(generate_assignments, [self.perturb(x[mask].detach()) for _ in range(num_mc_samples)])
                    for sample, (ass_s, conc_ass_s, dist_s, prob_s, batch_s, self.last_num_clusters) in enumerate(res):
                        assignments[sample * batch_size:(sample + 1) * batch_size] = ass_s
                        concept_assignments[sample * batch_size:(sample + 1) * batch_size] = conc_ass_s
                        batch = torch.cat((batch, batch_s), dim=0)
                except:
                    self.pool.shutdown()
                    exit()
        adj = adj.repeat(num_mc_samples, 1, 1)


        if self.use_centroids_as_embedding:
            scattered_x = self.cluster_alg.centroids[concept_assignments].repeat(num_mc_samples, 1, 1)
            # [batch_size * num_mc_samples, max_num_clusters] (repeat: [batch_size * num_mc_samples, num_nodes_max, num_features])
            # definitely not the most efficient way, as reduce is useless, just need inverse
            x_new = scatter(scattered_x, assignments[:, :, None], reduce="mean", dim=-2)[:, 1:, :]
        else:
            # [batch_size * num_mc_samples, max_num_clusters] (repeat: [batch_size * num_mc_samples, num_nodes_max, num_features])
            x_new = scatter(x.repeat(num_mc_samples, 1, 1), assignments[:, :, None], reduce="mean", dim=-2)[:, 1:, :]
        if self.final_bottleneck is not None:
            # Because both transformations are linear, this should be equivalent to applying it before pooling
            x_new = self.final_bottleneck(x_new)
        # [batch_size * num_mc_samples, max_num_nodes, max_num_clusters]: for each node: all clusters it points to (with index 0 (masked nodes) removed)
        adj_new = scatter(adj, assignments[:, :, None], reduce="max", dim=-2)[:, 1:, :]
        # [batch_size * num_mc_samples, max_num_clusters, max_num_clusters]: for each cluster: all clusters it points to  (with index 0 (masked nodes) removed)
        adj_new = scatter(adj_new, assignments[:, None, :], reduce="max", dim=-1)[:, :, 1:]

        # [batch_size * num_mc_samples] Note that this gives the number of clusters, not the index because 0 is the placeholder for masked nodes
        num_clusters, _ = torch.max(assignments, dim=-1)
        # [batch_size * num_mc_samples, max_num_clusters]: True iff cluster/new node index is valid / less than the number of clusters in that batch element
        mask_new = torch.arange(x_new.shape[-2], device=custom_logger.device)[None, :] < num_clusters[:, None]
        if self.clustering_loss_weight == 0:
            clustering_loss = 0
        else:
            clustering_loss = self.clustering_loss_weight * torch.linalg.vector_norm(torch.min(distances, dim=-1)[0])
            if clustering_loss >= 10:
                # Cap clustering loss at 10 to avoid numerical instability
                clustering_loss /= (clustering_loss.detach() / 10)
        return x_new, adj_new, None, probabilities, clustering_loss, concept_assignments, assignments, x, mask_new

    def forward(self, x: torch.Tensor, adj: torch.Tensor, mask=None, edge_weights=None):
        assert self.transparency == 1
        return self.hard_fn(self.preprocess(x, adj, mask, edge_weights), adj, mask, edge_weights)

    def log_data(self, epoch: int, index: int):
        log({f"num_clusters_{index}": self.last_num_clusters}, step=epoch)

    def log_assignments(self, model: CustomNet, data: Data, num_graphs_to_log: int, epoch: int):
        # TODO adjust visualizations for other graphs to new signature
        # IMPORTANT: Here it is crucial to have batches of the size used during training in the forward pass
        # if using only a single example, some concepts might not be present but we still enforce the same number of
        # clusters
        TEMPERATURE = 0.1  # softmax temperature that makes clearer which node is assigned to which cluster
        SAMPLES_PER_CONCEPT = 3
        node_table = wandb.Table(["graph", "pool_step", "node_index", "r", "g", "b", "border_color", "label", "activations"])
        edge_table = wandb.Table(["graph", "pool_step", "source", "target"])
        concept_node_tables = {}
        concept_edge_tables = {}
        concept_purity_table = wandb.Table(["pool_step", "concept", "top-graph", "occurrences"])
        device = custom_logger.device
        with torch.no_grad():
            data = data.clone().detach().to(device)
            # concepts: [batch_size, max_num_nodes_final_layer, embedding_dim_out_final_layer] the node embeddings of the final graph
            out, _, concepts, _, info = model(data, collect_info=True)
            pool_activations = [data.x] + info.pooling_activations
            adjs = [data.adj] + info.adjs_or_edge_indices
            masks = [data.mask] + info.all_batch_or_mask
            input_embeddings = [data.x] + info.input_embeddings
            pool_assignments = [info.pooling_assignments[i] for i in range(len(info.pooling_assignments))
                                if not isinstance(model.graph_network.pool_blocks[i], DenseNoPoolBlock)]
            centroids = [torch.eye(data.x.shape[-1], device=custom_logger.device)] + \
                        [pb.cluster_alg.centroids.detach() if hasattr(pb, "cluster_alg") else None for pb in model.graph_network.pool_blocks]
            # for each pool block [num_nodes_in_total, num_centroids] with the distance of each node embedding (after GNN) to each centroid
            centroid_distances = [torch.cdist(pool_activations[pool_step + 1][masks[pool_step]], centroids[pool_step + 1])
                                  for pool_step in range(len(pool_assignments))]
            # for each pool block [num_nodes_in_total] with the concept that is assigned to each input node to the pool block
            initial_concepts = [torch.argmin(torch.cdist(input_embeddings[pool_step][masks[pool_step]], centroids[pool_step]), dim=-1)
                                for pool_step in range(len(pool_assignments))]

            ############################## Log distance to centroid distribution #########
            for pool_step in range(len(pool_assignments)):
                # [batch_size, max_num_nodes] with values in [-1, num_nodes_total] mapping each valid node index to its
                # overall index and all others to -1
                dense_to_sparse_maps = torch.full(masks[pool_step].shape, -1, dtype=torch.long,
                                                  device=custom_logger.device)
                dense_to_sparse_maps[masks[pool_step]] = torch.arange(dense_to_sparse_maps[masks[pool_step]].shape[0],
                                                                      device=custom_logger.device)


                pool_block = model.graph_network.pool_blocks[pool_step]
                temperature = getattr(pool_block, "soft_sampling", 0)
                # TODO at least for the paper I should reconsider doing this over the whole dataset instead of just one batch
                sorted_distances, _ = torch.sort(centroid_distances[pool_step], dim=-1)
                stds, means = torch.std_mean(sorted_distances, dim=0)
                medians, _ = torch.median(sorted_distances, dim=0)
                mins, _ = torch.min(sorted_distances, dim=0)
                maxs, _ = torch.max(sorted_distances, dim=0)
                dist_table = wandb.Table(["sortindex", "mean", "std", "median", "min", "max"])
                for i in range(stds.shape[0]):
                    dist_table.add_data(i, means[i].item(), stds[i].item(), medians[i].item(), mins[i].item(),
                                        maxs[i].item())
                log({f"centroid_distances_{pool_step}": dist_table}, step=epoch)

                ############################## Print Probability Distributions #########
                if temperature != 0:
                    # Note: the hard_assignments here are just a sanity check and should always agree. They can be
                    # removed for efficiency
                    hard_assignments = pool_block.cluster_alg.predict(pool_activations[pool_step + 1][masks[pool_step]])
                    max_probs, arg_max = torch.max(torch.nn.functional.softmin(centroid_distances[pool_step] / temperature, dim=-1), dim=-1)
                    print(f"\nProbability of most likely concept in pooling step {pool_step}: "
                          f"{100 * torch.mean(max_probs):.2f}%+-{100*torch.std(max_probs):.2f} with "
                          f"{100 * torch.sum(hard_assignments == arg_max) / arg_max.shape[0]:.2f}% of the soft maxima "
                          f"agreeing with the hard assignment")

                ############################## Log Graphs ##############################
                for graph_i in range(num_graphs_to_log):
                    # Calculate concept assignment colors
                    # [num_nodes] (with batch dimension and masked nodes removed)
                    assignment = pool_assignments[pool_step][graph_i][masks[pool_step][graph_i]].detach().cpu()
                    ColorUtils.ensure_min_rgb_colors(torch.max(assignment) + 1)
                    # [num_nodes, 3] (intermediate dimensions: num_nodes x num_clusters x 3)
                    concept_colors = ColorUtils.rgb_colors[assignment, :]

                    # Calculate feature colors
                    # [num_nodes_in_neighbourhood, num_concepts] where (i, j) gives difference between node i and concept j
                    feature_colors = torch.cdist(input_embeddings[pool_step][graph_i, masks[pool_step][graph_i]],
                                                 centroids[pool_step])
                    ColorUtils.ensure_min_rgb_feature_colors(feature_colors.shape[1])
                    feature_colors = torch.sum(torch.nn.functional.softmin(feature_colors / TEMPERATURE, dim=1)[:, :, None].cpu() *
                                               ColorUtils.rgb_feature_colors[None, :feature_colors.shape[1], :], dim=1)
                    feature_colors = torch.round(feature_colors).to(int)

                    for i, i_old in enumerate(masks[pool_step][graph_i].nonzero().squeeze(1)):
                        node_table.add_data(graph_i, pool_step, i, feature_colors[i, 0].item(),
                                            feature_colors[i, 1].item(), feature_colors[i, 2].item(),
                                            ColorUtils.rgb2hex_tensor(concept_colors[i, :]),
                                            f"Cluster {assignment[i]}",
                                            ", ".join([f"{m.item():.2f}" for m in
                                                       pool_activations[pool_step][graph_i, i_old, :].cpu()]))

                    edge_index, _, _ = adj_to_edge_index(adjs[pool_step][graph_i:graph_i+1, :, :],
                                                         masks[pool_step][graph_i:graph_i+1])
                    for i in range(edge_index.shape[1]):
                        edge_table.add_data(graph_i, pool_step, edge_index[0, i].item(), edge_index[1, i].item())

                assignment = pool_assignments[pool_step]


                # This is not ideal as we only sample concepts from the same (first) batch. But we could increase it's size
                # [batch_size, max_num_nodes] True iff. the connected component of a node has already been explored
                checked_mask = torch.zeros(*masks[pool_step].shape, dtype=torch.bool)
                for concept in torch.unique(assignment).tolist():
                    ####################### Log Example Concept Graphs #######################
                    if concept not in concept_node_tables:
                        concept_node_tables[concept] = wandb.Table(["graph", "pool_step", "node_index", "r", "g", "b",
                                                                    "border_color", "label", "activations"])
                        concept_edge_tables[concept] = wandb.Table(["graph", "pool_step", "source", "target"])
                    concept_node_table, concept_edge_table = concept_node_tables[concept], concept_edge_tables[concept]

                    # [num_nodes_with_concept_total, 2] with all pairs of (batch_index, node_index) of nodes that are
                    # not masked and are classified to a certain example
                    example_nodes = (torch.logical_and(masks[pool_step], assignment == concept)).nonzero()

                    # ################################ Log Concept Purity and Top Subgraphs ################################
                    # TODO this should ideally be somewhere where I pass over the whole graph
                    # to reduce the number of isomorphism tests, we first build up a WL hash table. The entry at each
                    # key contains a list of tuples of networkx graphs and the number of isomorphic graphs
                    buckets = {}
                    for sample, node in example_nodes:
                        if checked_mask[sample, node]:
                            continue
                        edge_index_prev, _, _ = adj_to_edge_index(adjs[pool_step][sample])
                        edge_index_prev = edge_index_prev[:, assignment[sample, edge_index_prev[0, :]] == assignment[
                            sample, edge_index_prev[1, :]]]
                        nodes_in_cur_graph = torch.sum(masks[pool_step][sample]).item()
                        subset, edge_index, mapping, _ = k_hop_subgraph(node.item(), nodes_in_cur_graph,
                                                                        edge_index_prev,
                                                                        relabel_nodes=True,
                                                                        num_nodes=nodes_in_cur_graph)
                        checked_mask[sample, mapping] = True

                        G = to_networkx(Data(concept=initial_concepts[pool_step][dense_to_sparse_maps[sample, subset]],
                                             edge_index=edge_index, num_nodes=subset.shape[0]),
                                        to_undirected=not self.directed_graphs,
                                        node_attrs=["concept"])
                        key = nx.algorithms.graph_hashing.weisfeiler_lehman_graph_hash(G, node_attr="concept")

                        if key in buckets:
                            for other_graph, occurrences in buckets[key].items():
                                # iso.generic_node_match("concept", None, list_close)
                                if nx.is_isomorphic(other_graph, G, node_match=iso.categorical_node_match("concept",
                                                                                                          None)):
                                    buckets[key][other_graph] = occurrences + 1
                                    break
                            else:
                                buckets[key][G] = 1
                        else:
                            buckets[key] = {G: 1}

                    occurrences = []
                    for bucket in buckets.values():
                        occurrences += bucket.items()

                    samples_seen = 0
                    for top_k, (subgraph, occ) in enumerate(sorted(occurrences, key=lambda k: k[1], reverse=True)):
                        concept_purity_table.add_data(pool_step, concept, top_k, occ)
                        if samples_seen < SAMPLES_PER_CONCEPT:
                            ################## Log Top Subgraphs in Concept #################
                            for i, node_concept in nx.get_node_attributes(subgraph, "concept").items():
                                ColorUtils.ensure_min_rgb_colors(node_concept)
                                concept_node_table.add_data(samples_seen, pool_step, i, ColorUtils.rgb_colors[node_concept][0],
                                                            ColorUtils.rgb_colors[node_concept][1],
                                                            ColorUtils.rgb_colors[node_concept][2],
                                                            "#FFF", # as all nodes are mapped to the same concept, it doesn't matter which one was selected"#F00" if subset[i] == node.item() else "#FFF",
                                                            # This gives insights how likely it would be that the node would have been mapped to another centroid
                                                            # BUT would require either storing the "subset" mapping or the lables directly in the graph. Do if necessary.
                                                            "", # "Distances: " + ", ".join([f"{m.item():.2f}" for m in centroid_distances[pool_step][i, :].cpu()]),
                                                            "")
                            for (from_node, to_node) in subgraph.edges:
                                concept_edge_table.add_data(samples_seen, pool_step, from_node, to_node)
                            samples_seen += 1

        log({f"concept_purity_table": concept_purity_table}, step=epoch)

        for concept in concept_node_tables.keys():
            log({
                f"concept_node_table_{concept}": concept_node_tables[concept],
                f"concept_edge_table_{concept}": concept_edge_tables[concept]
            }, step=epoch)
        log(dict(
            # graphs_table=graphs_table
            node_table=node_table,
            edge_table=edge_table
        ), step=epoch)

    def end_epoch(self):
        if not self.global_clusters:
            return
        self.use_global_clusters = True
        self.cluster_alg.fit(self.seen_embeddings.detach(), train=True)
        self.seen_embeddings = torch.empty((0, self.num_output_features), device=custom_logger.device)

    @PoolBlock.output_dim.getter
    def output_dim(self):
        return self.embedding_sizes[-1] if self.final_bottleneck_dim is None else self.final_bottleneck_dim


__all_dense__ = [DenseNoPoolBlock, DiffPoolBlock, MonteCarloBlock]
__all_sparse__ = [ASAPBlock, SparseNoPoolBlock]


def from_name(name: str, dense_data: bool):
    for b in __all_dense__ if dense_data else __all_sparse__:
        if b.__name__ == name + "Block":
            return b
    raise ValueError(f"Unknown pooling type {name} for dense_data={dense_data}!")#


def valid_names() -> List[str]:
    return [b.__name__[:-5] for b in __all_dense__] + [b.__name__[:-5] for b in __all_sparse__]
