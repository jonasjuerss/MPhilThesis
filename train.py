import argparse
import json
import os
import typing
from contextlib import nullcontext
from datetime import datetime
from multiprocessing import Process

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn.functional as F
import wandb
from sklearn.decomposition import PCA
from sklearn.manifold import TSNE
from torch_geometric.loader import DataLoader, DenseDataLoader
from torch_geometric.nn import DenseGCNConv, GCNConv
from tqdm import tqdm
from typing import Union
import plotly.express as px

import custom_logger
import output_layers
import poolblocks.poolblock
from custom_logger import log
from custom_net import CustomNet
from data_generation.custom_dataset import UniqueMotifCategorizationDataset, CustomDataset, \
    UniqueMultipleOccurrencesMotifCategorizationDataset
from data_generation.dataset_wrappers import DatasetWrapper, CustomDatasetWrapper, TUDatasetWrapper, EnzymesWrapper
from data_generation.deserializer import from_dict
from data_generation.motifs import BinaryTreeMotif, HouseMotif, FullyConnectedMotif

DENSE_CONV_TYPES = [DenseGCNConv]
SPARSE_CONV_TYPES = [GCNConv]

def train_test_epoch(train: bool, model: CustomNet, optimizer, loader: Union[DataLoader, DenseDataLoader], epoch: int,
                     pooling_loss_weight: float, dense_data: bool):
    if train:
        model.train()
    correct = 0
    sum_loss = 0
    sum_classification_loss = 0
    sum_pooling_loss = 0
    num_classes = model.output_layer.num_classes
    class_counts = torch.zeros(num_classes)
    with nullcontext() if train else torch.no_grad():
        for data in loader:
            data = data.to(device)
            batch_size = data.y.size(0)
            if train:
                optimizer.zero_grad()

            out, _, pooling_loss = model(data)
            target = data.y
            if dense_data:
                # For some reason, DataLoader flattens y (e.g. for batch_size=64 and output size 2, it would create one
                # vector of 128 entries). DenseDataLoader doesn't show this behaviour which is why we squeeze in our
                # training loop. As long as we only do graph classification (as opposed to predicting multiple values
                # per graph), we can fix this by just manually introducing the desired dimension with unsqueeze. In the
                # future, we might just use reshape instead of unsqueeze to support multiple output values, but the
                # question, why pytorch geometric behaves this way remains open.
                target = target.squeeze(1)
            classification_loss = F.nll_loss(out, target)
            loss = classification_loss + pooling_loss_weight * pooling_loss + model.custom_losses(batch_size)

            sum_loss += batch_size * float(loss)
            sum_classification_loss += batch_size * float(classification_loss)
            sum_pooling_loss += batch_size * float(pooling_loss)
            pred_classes = out.argmax(dim=1)
            correct += int((pred_classes == target).sum())
            class_counts += torch.bincount(pred_classes.detach(), minlength=num_classes).cpu()

            if train:
                loss.backward()
                optimizer.step()
    dataset_len = len(loader.dataset)
    mode = "train" if train else "test"
    model.log_custom_losses(mode, epoch, dataset_len)
    distr_dict = {}
    class_counts /= dataset_len
    if not train:
        distr_dict = {f"{mode}_percentage_class_{i}": class_counts[i] for i in range(num_classes)}
    log({f"{mode}_loss": sum_loss / dataset_len,
         f"{mode}_pooling_loss": sum_pooling_loss / dataset_len,
         f"{mode}_classification_loss": sum_classification_loss / dataset_len,
         f"{mode}_accuracy": correct / dataset_len, **distr_dict},
        step=epoch)
    model.eval()  # make sure model is always in eval by default


def log_formulas(model: CustomNet, train_loader: DataLoader, test_loader: DataLoader, class_names: typing.List[str],
                epoch: int):
    model.explain(train_loader, test_loader, class_names)

def process_embeddings(embs, epoch, run):
    tsne = TSNE(n_components=2)
    # pca = PCA(n_components=2)
    for pool_step, emb in enumerate(embs):
        emb = torch.cat(emb, dim=0).detach().numpy()
        coords = tsne.fit_transform(X=emb)
        # for row in range(coords.shape[0]):
        # table.add_data(pool_step, *coords[row], "#000", "")
        fig = px.scatter(x=coords[:, 1], y=coords[:, 0]) # , size=4
        fig.update_traces(marker={'size': 4})
        # path = os.path.join(save_path, f"scatter_{pool_step}.html")
        # fig.write_html(path, auto_play=False)
        # log({f"scatter_{pool_step}": wandb.Html(path)}, step=epoch)
        log({f"embeddings_{pool_step}": fig}, _run=run, step=epoch)

def log_embeddings(model: CustomNet, data_loader: DataLoader, dense_data: bool, epoch: int, save_path):
    # table too big to load (wandb only shows 10000 entries)
    # table = wandb.Table(columns=["pool_step", "x", "y", "point_color", "label"])
    with torch.no_grad():
        if dense_data:
            # list: [num_pool_layers, num_batches] with entries [num_nodes_total_batch, layer_sizes[pool_ste][-1]]
            embs = [[] for _ in model.graph_network.pool_blocks]
            for data in data_loader:
                data.to(custom_logger.device)
                _, _, _, _, pool_activations, _, masks = model(data)
                masks = [data.mask] + masks
                for i, act in enumerate(pool_activations):
                    embs[i].append(act[masks[i]].cpu())
            # list [num_pool_layers] with entries [num_nodes_total, layer_sizes[pool_ste][-1]]
            # TSNE takes some time, so we can let this happen asynchronously
            Process(target=process_embeddings, args=(embs, epoch, wandb.run)).start()

            #log(dict(embeddings=table), step=epoch)
        else:
            print("Logging embeddings not implemented for sparse data yet!")


num_colors = 2
current_dataset = UniqueMultipleOccurrencesMotifCategorizationDataset(BinaryTreeMotif(5, [0], num_colors),
                                                                      [HouseMotif([1], [1], num_colors),
                                                                       FullyConnectedMotif(5, [1], num_colors)],
                                                                      [[0.25, 0.25, 0.25, 0.25], [0.25, 0.25, 0.25, 0.25]])
                                                                        # [[0.4, 0.6], [0.4, 0.6]])#
current_dataset_wrapper = CustomDatasetWrapper(current_dataset)
current_dataset_wrapper = EnzymesWrapper()
def parse_json_str(s: str):
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ["\"", "'"]:
        s = s[1:-2] # remove possible quotation marks around whole json
    return json.loads(s)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--lr', type=float, default=0.01,
                        help='The Adam learning rate to use.')
    parser.add_argument('--pooling_loss_weight', type=float, default=0.5,
                        help='The weight of the pooling loss.')
    parser.add_argument('--entropy_loss_weight', type=float, default=0,
                        help='The weight of the entropy loss in the explanation layer.')
    parser.add_argument('--wd', type=float, default=5e-4,
                        help='The Adam weight decay to use.')
    parser.add_argument('--num_epochs', type=int, default=10000,
                        help='The number of epochs to train for.')
    parser.add_argument('--batch_size', type=int, default=64,
                        help='The batch size to use.')
    parser.add_argument('--add_layer', type=int, nargs='+', action='append',
                        default=[[16, 16], [16, 16, 4]], dest='layer_sizes',
                        help='The layer sizes to use. Example: --add_layer 16 32 --add_layer 32 64 16 results in a '
                             'network with 2 pooling steps where 5 message passes are performed before the first and ')
    parser.add_argument('--pool_blocks', type=parse_json_str, nargs='+',
                        # default=[{"num_output_layers": [4]}],
                        # default=[{"num_output_nodes": 8}],
                        # default=[{"num_concepts": 3}],
                        # default=[],
                        dest='pool_block_args',
                        help="Additional arguments for each pool block")
    # parser.add_argument('--nodes_per_layer', type=int, default=[4],
    #                     help='The number of nodes after each pooling step for architectures like DiffPool which require'
    #                          ' to pre-specify that. Note that the last one should be 1 for classification')

    parser.add_argument('--conv_type', type=str, default="DenseGCNConv", choices=["DenseGCNConv", "GCNConv"],
                        help='The type of graph convolution to use.')
    parser.add_argument('--output_layer', type=str, default="DenseClassifier",
                        help='The type of graph convolution to use.')
    # TODO sum might be too weak, implement Pietro's global pool
    parser.add_argument('--output_layer_merge', type=str, default="flatten", choices=["flatten", "none", "sum", "avg"],
                        help='How to merge the output encodings of all nodes after the last pooling step for the final '
                             'classification layer. \"flatten\" only works if the number of clusters in the last graph '
                             'is constant/independent of the input graph size and \"none\" only if the chosen '
                             'classifier can deal with a set of inputs.')
    parser.add_argument('--pooling_type', type=str, default="Perturbed", choices=poolblocks.poolblock.valid_names(),
                        help='The type of pooling to use.')

    parser.add_argument('--dataset', type=parse_json_str, default=current_dataset_wrapper.__dict__(),
                        help="A json that defines the current dataset")
    parser.add_argument('--min_nodes', type=int, default=0,
                        help='Minimum number of nodes for a graph in the dataset. All other graphs are discarded. '
                             'Required e.g. to guarantee that ASAPooling always outputs a fixed number of nodes when '
                             'num_output_nodes is set.')

    parser.add_argument('--train_split', type=float, default=0.8,
                        help='Fraction of samples used for the train set.')

    parser.add_argument('--graph_log_freq', type=int, default=50,
                        help='Every how many epochs to log graphs to wandb. The final predictions will always be '
                             'logged, except for if this is negative.')
    parser.add_argument('--formula_log_freq', type=int, default=50,
                        help='Every how many epochs to log explanations to wandb. The final predictions will always be '
                             'logged, except for if this is negative.')
    parser.add_argument('--graphs_to_log', type=int, default=6,
                        help='How many graphs from the training and testing set to log.')
    parser.add_argument('--forced_embeddings', type=float, default=None,
                        help='For debugging. If set, embeddings will not be calculated. Instead, all embeddings of '
                             'nodes with neighbours will be set to the given number and all nodes without neighbours '
                             'will have embedding 0.')
    parser.add_argument('--gnn_activation', type=str, default="leaky_relu",
                        help='Activation function to be used in between the GNN layers')

    parser.set_defaults(dense_data=True)
    parser.add_argument('--sparse_data', action='store_false', dest='dense_data',
                        help='Switches from a dense representation of graphs (dummy nodes are added so that each of '
                             'them has the same number of nodes) to a sparse one.')
    parser.add_argument('--dense_data', action='store_true', dest='dense_data',
                        help='Switches from a sparse data representation of graphs to a dense one (dummy nodes are '
                             'added so that each of them has the same number of nodes).')

    parser.add_argument('--seed', type=int, default=1,
                        help='The seed used for pytorch. This also determines the dataset if generated randomly.')
    parser.add_argument('--save_path', type=str,
                        default=os.path.join("models", datetime.now().strftime("%d-%m-%Y_%H-%M-%S")),
                        help='The path to save the checkpoint to. Will be models/dd-mm-YY_HH-MM-SS.pt by default.')
    parser.add_argument('--device', type=str, default="cuda",
                        help='The device to train on. Allows to use CPU or different GPUs.')
    parser.set_defaults(use_wandb=True)
    parser.add_argument('--no_wandb', action='store_false', dest='use_wandb',
                        help='Turns off logging to wandb')
    args = parser.parse_args()

    if os.path.exists(args.save_path):
        raise ValueError(f"Checkpoint path already exists: {args.save_path}!")
    else:
        os.makedirs(args.save_path)

    for block_args in args.pool_block_args:
        if args.pooling_type in ["ASAP"] and block_args.get("num_output_nodes", -1) > args.min_nodes:
            print(f"The pooling method {args.pooling_type} cannot increase the number of nodes. Increasing "
                  f"min_nodes to {block_args['num_output_nodes']} to guarantee the given fixed number of output nodes.")
            args.min_nodes = block_args["num_output_nodes"]
    args = custom_logger.init(args)

    device = torch.device(args.device)
    custom_logger.device = device
    torch.manual_seed(args.seed)

    dataset_wrapper = typing.cast(DatasetWrapper, from_dict(args.dataset))
    dataset = dataset_wrapper.get_dataset(args.dense_data, args.min_nodes)
    num_train_samples = int(args.train_split * len(dataset))
    train_data = dataset[:num_train_samples]
    test_data = dataset[num_train_samples:]

    if args.dense_data:
        train_loader = DenseDataLoader(train_data, batch_size=args.batch_size, shuffle=True)
        test_loader = DenseDataLoader(test_data, batch_size=args.batch_size, shuffle=True)
        log_graph_loader = DenseDataLoader(test_data[:args.batch_size], batch_size=args.batch_size, shuffle=False)

    else:
        train_loader = DataLoader(train_data, batch_size=args.batch_size, shuffle=True)
        test_loader = DataLoader(test_data, batch_size=args.batch_size, shuffle=True)
        log_graph_loader = DataLoader(test_data[:args.batch_size], batch_size=args.batch_size, shuffle=False)

    # Get last (and only data batch from log_graph_loader)
    for graphs_to_log in log_graph_loader:
        pass

    CONV_TYPES = DENSE_CONV_TYPES if args.dense_data else SPARSE_CONV_TYPES
    conv_type = next((x for x in CONV_TYPES if x.__name__ == args.conv_type), None)
    if conv_type is None:
        raise ValueError(f"No convolution type named \"{args.conv_type}\" found for dense_data={args.dense_data}!")
    output_layer = output_layers.from_name(args.output_layer)
    gnn_activation = getattr(torch.nn.functional, args.gnn_activation)
    model = CustomNet(dataset_wrapper.num_node_features, dataset_wrapper.num_classes, args=args, device=device,
                      output_layer_type=output_layer,
                      pooling_block_type=poolblocks.poolblock.from_name(args.pooling_type, args.dense_data),
                      conv_type=conv_type, activation_function=gnn_activation).to(device)

    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.wd)

    for epoch in tqdm(range(args.num_epochs)):
        train_test_epoch(True, model, optimizer, train_loader, epoch, args.pooling_loss_weight, args.dense_data)
        if epoch % args.graph_log_freq == 0:
            model.graph_network.pool_blocks[0].log_assignments(model, graphs_to_log, args.graphs_to_log, epoch)
            # log_embeddings(model, train_loader, args.dense_data, epoch, args.save_path)
        if epoch % args.formula_log_freq == 0:
            log_formulas(model, train_loader, test_loader, dataset_wrapper.class_names, epoch)
        train_test_epoch(False, model, optimizer, test_loader, epoch, args.pooling_loss_weight, args.dense_data)
        model.end_epoch()

    if args.graph_log_freq >= 0:
        model.graph_network.pool_blocks[0].log_assignments(model, graphs_to_log, args.graphs_to_log, epoch)
    if args.formula_log_freq >= 0:
        log_formulas(model, train_loader, test_loader, dataset_wrapper.class_names, epoch)

