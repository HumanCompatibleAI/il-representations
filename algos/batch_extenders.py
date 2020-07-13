from abc import ABC
import torch
from torch.distributions import Normal

"""
BatchExtenders are used in situations where you want to pass a batch forward for loss that is different than the 
batch seen by your encoder. The currently implemented situation where this is the case is Momentum, where you want 
to pass forward a bunch of negatives from prior encoding runs to increase the difficulty of your prediction task. 
One might also imagine this being useful for doing trajectory-mixing in a RNN case where batches naturally need 
to be all from a small number of trajectories, but this isn't yet implemented. 
"""


class BatchExtender(ABC):
    # TODO Is there a better way to optionally allow for more arguments ignored by the children?
    def __init__(self, **kwargs):
        pass

    def __call__(self, context_dist, target_dist):
        pass


class IdentityBatchExtender(BatchExtender):
    def __call__(self, contexts, targets):
        return contexts, targets


class QueueBatchExtender(BatchExtender):
    def __init__(self, queue_size, queue_dim, sample=False):
        super(QueueBatchExtender, self).__init__()
        self.queue_size = queue_size
        self.representation_dim = queue_dim
        self.sample = sample
        self.queue_loc = torch.randn(self.queue_size, self.representation_dim)
        self.queue_scale = torch.randn(self.queue_size, self.representation_dim)
        self.queue_ptr = 0

    def __call__(self, context_dist, target_dist):
        # Call up current contents of the queue, duplicate. Add targets to the queue,
        # potentially overriding old information in the process. Return targets concatenated to contents of queue
        targets_loc = target_dist.loc
        targets_scale = target_dist.scale
        batch_size = targets_loc.shape[0]
        queue_targets_scale = self.queue_scale.clone().detach()
        queue_targets_loc = self.queue_loc.clone().detach()
        self.queue_loc[self.queue_ptr:self.queue_ptr + batch_size] = targets_loc
        self.queue_scale[self.queue_ptr:self.queue_ptr + batch_size] = targets_scale
        self.queue_ptr = (self.queue_ptr + batch_size) % self.queue_size
        merged_loc = torch.cat([targets_loc, queue_targets_loc], dim=0)
        merged_scale = torch.cat([targets_scale, queue_targets_scale], dim=0)
        merged_target_dist = Normal(loc=merged_loc, scale=merged_scale)

        return context_dist, merged_target_dist
