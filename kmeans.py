import math
from typing import Optional

import torch
from time import time
import numpy as np
from torch.nn import UninitializedBuffer
from torch_scatter import scatter

import graphutils

"""
Modified from: https://github.com/DeMoriarty/fast_pytorch_kmeans
"""

class KMeans(torch.nn.Module):
    '''
    Kmeans clustering algorithm implemented with PyTorch

    Parameters:
      n_clusters: int,
        Number of clusters

      max_iter: int, default: 100
        Maximum number of iterations

      tol: float, default: 0.0001
        Tolerance

      verbose: int, default: 0
        Verbosity

      mode: {'euclidean', 'cosine'}, default: 'euclidean'
        Type of distance measure

      minibatch: {None, int}, default: None
        Batch size of MinibatchKmeans algorithm
        if None perform full KMeans algorithm

    Attributes:
      centroids: torch.Tensor, shape: [n_clusters, n_features]
        cluster centroids
    '''

    def __init__(self, n_clusters, max_iter=100, tol=0.0001, verbose=0, mode="euclidean", minibatch=None, threshold=0,
                 centroids: Optional[torch.Tensor] = None):
        super().__init__()
        self.n_clusters = n_clusters
        self.max_iter = max_iter
        self.tol = tol
        self.verbose = verbose
        self.mode = mode
        self.minibatch = minibatch
        self.threshold = threshold
        self._loop = False
        self._show = False
        if self.threshold != 0 and self.mode != 'euclidean':
            raise NotImplementedError("A threshold for KMeans is currently on other distance functions than "
                                      "euclidean can easily be implemented by replacing cdist!")
        try:
            import PYNVML
            self._pynvml_exist = True
        except ModuleNotFoundError:
            self._pynvml_exist = False
        self.register_buffer("centroids", centroids)

    @staticmethod
    def cos_sim(a, b):
        """
          Compute cosine similarity of 2 sets of vectors

          Parameters:
          a: torch.Tensor, shape: [m, n_features]

          b: torch.Tensor, shape: [n, n_features]
        """
        a_norm = a.norm(dim=-1, keepdim=True)
        b_norm = b.norm(dim=-1, keepdim=True)
        a = a / (a_norm + 1e-8)
        b = b / (b_norm + 1e-8)
        return a @ b.transpose(-2, -1)

    @staticmethod
    def euc_sim(a, b):
        """
          Compute euclidean similarity of 2 sets of vectors

          Parameters:
          a: torch.Tensor, shape: [m, n_features]

          b: torch.Tensor, shape: [n, n_features]
        """
        # Returns the negative euclidean distance calculated as -\sum_i (a_i - b_i)^2 =- \sum_i a_i^2 + b_i^2 - a_i * b_i
        return 2 * a @ b.transpose(-2, -1) - (a ** 2).sum(dim=1)[..., :, None] - (b ** 2).sum(dim=1)[..., None, :]

    def remaining_memory(self):
        """
          Get remaining memory in gpu
        """
        torch.cuda.synchronize()
        torch.cuda.empty_cache()
        if self._pynvml_exist:
            pynvml.nvmlInit()
            gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            info = pynvml.nvmlDeviceGetMemoryInfo(gpu_handle)
            remaining = info.free
        else:
            remaining = torch.cuda.memory_allocated()
        return remaining

    def max_sim(self, a, b):
        """
          Compute maximum similarity (or minimum distance) of each vector
          in a with all of the vectors in b

          Parameters:
          a: torch.Tensor, shape: [m, n_features]

          b: torch.Tensor, shape: [n, n_features]
        """
        device = a.device.type
        batch_size = a.shape[0]
        if self.mode == 'cosine':
            sim_func = self.cos_sim
        elif self.mode == 'euclidean':
            sim_func = self.euc_sim

        sim = sim_func(a, b)
        max_sim_v, max_sim_i = sim.max(dim=-1)
        return max_sim_v, max_sim_i, sim

        # In our case, the clusters should always fit in GPU memory
        # if device == 'cpu':
        #     sim = sim_func(a, b)
        #     max_sim_v, max_sim_i = sim.max(dim=-1)
        #     return max_sim_v, max_sim_i
        # else:
        #     if a.dtype == torch.double:
        #         expected = a.shape[0] * a.shape[1] * b.shape[0] * 8
        #     if a.dtype == torch.float:
        #         expected = a.shape[0] * a.shape[1] * b.shape[0] * 4
        #     elif a.dtype == torch.half:
        #         expected = a.shape[0] * a.shape[1] * b.shape[0] * 2
        #     ratio = math.ceil(expected / self.remaining_memory())
        #     subbatch_size = math.ceil(batch_size / ratio)
        #     msv, msi = [], []
        #     for i in range(ratio):
        #         if i * subbatch_size >= batch_size:
        #             continue
        #         sub_x = a[i * subbatch_size: (i + 1) * subbatch_size]
        #         sub_sim = sim_func(sub_x, b)
        #         sub_max_sim_v, sub_max_sim_i = sub_sim.max(dim=-1)
        #         del sub_sim
        #         msv.append(sub_max_sim_v)
        #         msi.append(sub_max_sim_i)
        #     if ratio == 1:
        #         max_sim_v, max_sim_i = msv[0], msi[0]
        #     else:
        #         max_sim_v = torch.cat(msv, dim=0)
        #         max_sim_i = torch.cat(msi, dim=0)
        #     return max_sim_v, max_sim_i

    def fit_predict(self, X, centroids=None):
        """
          Combination of fit() and predict() methods.
          This is faster than calling fit() and predict() seperately.

          Parameters:
          X: torch.Tensor, shape: [n_samples, n_features]

          centroids: {torch.Tensor, None}, default: None
            if given, centroids will be initialized with given tensor
            if None, centroids will be randomly chosen from X

          Return:
          labels: torch.Tensor, shape: [n_samples]
        """
        assert isinstance(X, torch.Tensor), "input must be torch.Tensor"
        assert X.dtype in [torch.half, torch.float, torch.double], "input must be floating point"
        assert X.ndim == 2, "input must be a 2d tensor with shape: [n_samples, n_features] "

        batch_size, emb_dim = X.shape
        device = X.device.type
        start_time = time()
        if centroids is None:
            self.centroids = X[np.random.choice(batch_size, size=[self.n_clusters], replace=False)].detach()
        else:
            self.centroids = centroids
        # num_points_in_clusters = torch.ones(self.n_clusters, device=device, dtype=X.dtype)
        closest = None
        for i in range(self.max_iter):
            # iter_time = time()
            if self.minibatch is not None:
                x = X[np.random.choice(batch_size, size=[self.minibatch], replace=False)]
            else:
                x = X
            _, closest, neg_distances = self.max_sim(a=x, b=self.centroids)
            # matched_clusters, counts = closest.unique(return_counts=True)

            c_grad = torch.zeros_like(self.centroids)
            if self._loop:
                raise RuntimeError()
                # for j, count in zip(matched_clusters, counts):
                #     c_grad[j] = x[closest == j].sum(dim=0) / count
            else:
                if self.minibatch is None:
                    # [n_clusters, n_points] of cluster ids
                    expanded_closest = closest[None].expand(self.n_clusters, -1)
                    mask = (expanded_closest == torch.arange(self.n_clusters, device=device)[:, None]).to(X.dtype)
                    c_grad = mask @ x / mask.sum(-1)[..., :, None]
                    c_grad[c_grad != c_grad] = 0  # remove NaNs
                else:
                    pass
                    # expanded_closest = closest[None].expand(len(matched_clusters), -1)
                    # mask = (expanded_closest == matched_clusters[:, None]).to(X.dtype)

            error = (c_grad - self.centroids).pow(2).sum()
            cost = (c_grad[closest] - x).pow(2).sum()
            # if self.minibatch is not None:
            #     lr = 1 / num_points_in_clusters[:, None] * 0.9 + 0.1
            #     # lr = 1/num_points_in_clusters[:,None]**0.1
            # else:
            #     lr = 1
            # num_points_in_clusters[matched_clusters] += counts
            self.centroids = c_grad
            # self.centroids = self.centroids * (1 - lr) + c_grad * lr
            # if self.verbose >= 2:
            #     print('iter:', i, 'error:', error.item(), 'time spent:', round(time() - iter_time, 4))
            if error <= self.tol:
                break

        if self.threshold != 0:
            centroid_dists = torch.cdist(self.centroids, self.centroids)
            merge_mask = centroid_dists < self.threshold * torch.max(centroid_dists)
            # Note: there might be chains of centroids a-b-c, where dist(a, b), dist(b, c) < threshold,
            # but dist(a, c) > threshold. We decide to merge those by perfomring a connected component search on a graph
            # where there is an edge between 2 clusters iff. they are closer than the threshold.
            # [num_clusters] with values in [0, num_merged_clusters - 1]
            assignments = graphutils.dense_components(merge_mask[None, :, :],
                                                      torch.ones(self.n_clusters, dtype=torch.bool,
                                                                 device=device)[None, :],
                                                      is_directed=False).squeeze(0) - 1
            closest = assignments[closest]
            self.centroids = scatter(X, closest, dim=-2, reduce="mean")

        if self.verbose >= 1:
            print(
                f'used {i + 1} iterations ({round(time() - start_time, 4)}s) to cluster {batch_size} items into {self.n_clusters} clusters')
        return closest, cost

    def predict(self, X):
        """
          Predict the closest cluster each sample in X belongs to

          Parameters:
          X: torch.Tensor, shape: [n_samples, n_features]

          Return:
          labels: torch.Tensor, shape: [n_samples]
        """
        assert isinstance(X, torch.Tensor), "input must be torch.Tensor"
        assert X.dtype in [torch.half, torch.float, torch.double], "input must be floating point"
        assert X.ndim == 2, "input must be a 2d tensor with shape: [n_samples, n_features] "

        return self.max_sim(a=X, b=self.centroids)[1]

    def fit(self, X, centroids=None):
        """
          Perform kmeans clustering

          Parameters:
          X: torch.Tensor, shape: [n_samples, n_features]
        """
        assert isinstance(X, torch.Tensor), "input must be torch.Tensor"
        assert X.dtype in [torch.half, torch.float, torch.double], "input must be floating point"
        assert X.ndim == 2, "input must be a 2d tensor with shape: [n_samples, n_features] "

        self.fit_predict(X, centroids)