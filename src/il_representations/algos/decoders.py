import torch.nn as nn
import copy
import torch
import torch.nn.functional as F
from il_representations.algos.utils import independent_multivariate_normal
from il_representations.algos.encoders import NETWORK_ARCHITECTURE_DEFINITIONS, compute_output_shape
import gym.spaces as spaces
from stable_baselines3.common.distributions import make_proba_distribution
import numpy as np
import math
import logging

"""
LossDecoders are meant to be mappings between the representation being learned, 
and the representation or tensor that is fed directly into the loss. In many cases, these are the 
same, and this will just be a NoOp. 

Some cases where it is different: 
- When you are using a Projection Head in your contrastive loss, and comparing similarities of vectors that are 
k >=1 nonlinear layers downstream from the actual representation you'll use in later tasks 
- When you're learning a VAE, and the loss is determined by how effectively you can reconstruct the image 
from a representation vector, the LossDecoder will handle that representation -> image mapping 
- When you're predicting actions given current and next state, you'll want to predict those actions given 
both the representation of the current state, and also information about the next state. This occasional
need for extra information beyond the central context state is why we have `extra_context` as an optional 
bit of data that pair constructors can return, to be passed forward for use here 
"""

#TODO change shape to dim throughout this file and the code


class LossDecoder(nn.Module):
    def __init__(self, representation_dim, projection_shape, sample=False):
        super().__init__()
        self.representation_dim = representation_dim
        self.projection_dim = projection_shape
        self.sample = sample

    def forward(self, z, traj_info, extra_context=None):
        pass

    # Calls to self() will call self.forward()
    def decode_target(self, z, traj_info, extra_context=None):
        return self(z, traj_info, extra_context=extra_context)

    def decode_context(self, z, traj_info, extra_context=None):
        return self(z, traj_info, extra_context=extra_context)

    def get_vector(self, z_dist):
        if self.sample:
            return z_dist.rsample()
        else:
            return z_dist.mean

    def ones_like_projection_dim(self, x):
        return torch.ones(size=(x.shape[0], self.projection_dim,), device=x.device)


class NoOp(LossDecoder):
    def forward(self, z, traj_info, extra_context=None):
        return z


class TargetProjection(LossDecoder):
    def __init__(self, representation_dim, projection_shape, sample=False, learn_scale=False):
        super(TargetProjection, self).__init__(representation_dim, projection_shape, sample)

        self.target_projection = nn.Sequential(nn.Linear(self.representation_dim, self.projection_dim))

    def decode_context(self, z_dist, traj_info, extra_context=None):
        return z_dist

    def decode_target(self, z_dist, traj_info, extra_context=None):
        z_vector = self.get_vector(z_dist)
        mean = self.target_projection(z_vector)
        return independent_multivariate_normal(mean, z_dist.stddev)


class ProjectionHead(LossDecoder):
    def __init__(self, representation_dim, projection_shape, sample=False, learn_scale=False):
        super(ProjectionHead, self).__init__(representation_dim, projection_shape, sample)

        self.shared_mlp = nn.Sequential(nn.Linear(self.representation_dim, 256),
                                      nn.ReLU(),
                                      nn.Linear(256, 256),
                                      nn.ReLU())
        self.mean_layer = nn.Linear(256, self.projection_dim)

        if learn_scale:
            self.scale_layer = nn.Linear(256, self.projection_dim)
        else:
            self.scale_layer = self.ones_like_projection_dim

    def forward(self, z_dist, traj_info, extra_context=None):
        z = self.get_vector(z_dist)
        shared_repr = self.shared_mlp(z)
        return independent_multivariate_normal(mean=self.mean_layer(shared_repr),
                                               stddev=torch.exp(self.scale_layer(shared_repr)))


class MomentumProjectionHead(LossDecoder):
    def __init__(self, representation_dim, projection_shape, sample=False, momentum_weight=0.99, learn_scale=False):
        super(MomentumProjectionHead, self).__init__(representation_dim, projection_shape, sample=sample)
        self.context_decoder = ProjectionHead(representation_dim, projection_shape,
                                              sample=sample, learn_scale=learn_scale)
        self.target_decoder = copy.deepcopy(self.context_decoder)
        for param in self.target_decoder.parameters():
            param.requires_grad = False
        self.momentum_weight = momentum_weight

    def decode_context(self, z_dist, traj_info, extra_context=None):
        return self.context_decoder(z_dist, traj_info, extra_context=extra_context)

    def decode_target(self, z_dist, traj_info, extra_context=None):
        """
        Encoder target/keys using momentum-updated key encoder. Had some thought of making _momentum_update_key_encoder
        a backwards hook, but seemed overly complex for an initial POC
        :param x:
        :return:
        """
        with torch.no_grad():
            self._momentum_update_key_encoder()
            decoded_z_dist = self.target_decoder(z_dist, traj_info, extra_context=extra_context)
            return independent_multivariate_normal(decoded_z_dist.mean.detach(),
                                                   decoded_z_dist.stddev.detach())

    @torch.no_grad()
    def _momentum_update_key_encoder(self):
        for param_q, param_k in zip(self.context_decoder.parameters(), self.target_decoder.parameters()):
            param_k.data = param_k.data * self.momentum_weight + param_q.data * (1. - self.momentum_weight)


class BYOLProjectionHead(MomentumProjectionHead):
    def __init__(self, representation_dim, projection_shape, momentum_weight=0.99, sample=False):
        super(BYOLProjectionHead, self).__init__(representation_dim, projection_shape,
                                                 sample=sample, momentum_weight=momentum_weight)
        self.context_predictor = ProjectionHead(projection_shape, projection_shape)

    def forward(self, z_dist, traj_info, extra_context=None):
        internal_dist = super().forward(z_dist, traj_info, extra_context=extra_context)
        prediction_dist = self.context_predictor(internal_dist, traj_info, extra_context=None)
        return independent_multivariate_normal(mean=F.normalize(prediction_dist.mean, dim=1),
                                               stddev=prediction_dist.stddev)

    def decode_target(self, z_dist, traj_info, extra_context=None):
        with torch.no_grad():
            prediction_dist = super().decode_target(z_dist, traj_info, extra_context=extra_context)
            return independent_multivariate_normal(F.normalize(prediction_dist.mean, dim=1),
                                                   prediction_dist.variance)


class ActionPredictionHead(LossDecoder):
    """
    A decoder that takes in two vector representations of frames
    (one in context, one in extra_context), and produces a prediction
    of the action taken in between the frames
    """
    def __init__(self, representation_dim, projection_shape, action_space, sample=False):
        super().__init__(representation_dim, projection_shape, sample)

        # Use Stable Baseline's logic for constructing a SB action_dist from an action space
        self.action_dist = make_proba_distribution(action_space)
        latents_to_dist_params = self.action_dist.proba_distribution_net(2*representation_dim)
        self.param_mappings = dict()

        # Logic to cover both the Gaussian case of mean/stddev and the Categorical case of logits
        if isinstance(latents_to_dist_params, tuple):
            self.param_mappings['mean_actions'] = latents_to_dist_params[0]
            self.param_mappings['log_std'] = latents_to_dist_params[1]
        else:
            self.param_mappings['action_logits'] = latents_to_dist_params


    def decode_context(self, z_dist, traj_info, extra_context=None):
        # vector representations of current and future frames
        z = self.get_vector(z_dist)
        z_future = self.get_vector(extra_context)
        print(z.device)
        print(z_future.device)
        import pdb; pdb.set_trace()
        # concatenate current and future frames together
        z_merged = torch.cat([z, z_future], dim=1)
        if 'action_logits' in self.param_mappings:
            self.action_dist.proba_distribution(self.param_mappings['action_logits'](z_merged))
        elif 'mean_actions' in self.param_mappings:
            self.action_dist.proba_distribution(self.param_mappings['mean_actions'](z_merged),
                                                self.param_mappings['log_std'])

        return self.action_dist

    def decode_target(self, z_dist, traj_info, extra_context=None):
        return z_dist


def compute_decoder_input_shape_from_encoder(observation_space, encoder_arch):
    mocked_layers = []
    # first apply convolution layers + flattening
    current_channels = observation_space.shape[0]
    for layer_spec in encoder_arch:
        mocked_layers.append(nn.Conv2d(current_channels, layer_spec['out_dim'],
                                               kernel_size=layer_spec['kernel_size'], stride=layer_spec['stride']))
        current_channels = layer_spec['out_dim']
    obs_shape = compute_output_shape(observation_space, mocked_layers)
    flattened_shape = np.prod(obs_shape)
    return flattened_shape, obs_shape


class PixelDecoder(LossDecoder):
    def __init__(self, representation_dim, projection_shape, observation_space,
                 action_representation_dim=None,sample=False, encoder_arch_key=None,
                 learn_scale=False, constant_stddev=0.1):

        assert isinstance(observation_space, spaces.Box)
        # Assert that the observation space is a 3D box
        assert len(observation_space.shape) == 3
        # Assert it's square (2 of the dimensions are identical)
        assert len(np.unique(observation_space.shape) == 2)
        # Mildly hacky; assumes that we have more square
        # dimensions in our pixel box than we do channels
        square_dim = np.max(np.unique(observation_space.shape))
        super().__init__(representation_dim, projection_shape, sample)
        encoder_arch_key = encoder_arch_key or "BasicCNN"
        self.encoder_arch = NETWORK_ARCHITECTURE_DEFINITIONS[encoder_arch_key]

        # Computes the number of dimensions that will come out of the final
        # (channels, shape, shape) output of the convolutional network, after flattening
        flattened_input_dim, self.full_input_shape = compute_decoder_input_shape_from_encoder(observation_space,
                                                                                          self.encoder_arch)
        logging.debug(f"Decoder input dims: {flattened_input_dim}")
        reversed_architecture = list(reversed(self.encoder_arch))

        logging.debug(f"Initial channels: {self.full_input_shape[0]}")
        logging.debug(f"Initial shape: {self.full_input_shape[1:]}")
        self.learn_scale = learn_scale
        assert constant_stddev >= 0, f"Standard deviation must be non-negative, you passed in {constant_stddev}"
        self.constant_stddev = constant_stddev
        self.action_representation_dim = action_representation_dim

        #https://github.com/AntixK/PyTorch-VAE/blob/master/models/vanilla_vae.py
        if self.action_representation_dim is not None:
            self.initial_layer = nn.Linear(self.action_representation_dim + representation_dim,
                                           flattened_input_dim)
        else:
            self.initial_layer = nn.Linear(representation_dim, flattened_input_dim)

        decoder_layers = []
        for i in range(len(reversed_architecture) - 1):
            padding = reversed_architecture[i].get('padding', 0)
            decoder_layers.append(nn.Sequential(
                                  nn.ConvTranspose2d(reversed_architecture[i]['out_dim'],
                                                     reversed_architecture[i+1]['out_dim'],
                                                     kernel_size=reversed_architecture[i]['kernel_size'],
                                                     stride=reversed_architecture[i]['stride'],
                                                     padding=padding),
                                  nn.BatchNorm2d(reversed_architecture[i+1]['out_dim']),
                                  nn.ReLU()))
        final_padding = reversed_architecture[i].get('padding', 0)
        decoder_layers.append(nn.Sequential(
                              nn.ConvTranspose2d(reversed_architecture[-1]['out_dim'],
                                                 reversed_architecture[-1]['out_dim'],
                                                 kernel_size=reversed_architecture[-1]['kernel_size'],
                                                 stride=reversed_architecture[-1]['stride'],
                                                 padding=final_padding),
                              nn.BatchNorm2d(reversed_architecture[-1]['out_dim']),
                              nn.ReLU())
        )
        decoder_layers.append(nn.Upsample((square_dim, square_dim)))

        self.decoder = nn.Sequential(*decoder_layers)
        # A final layer to produce each of mean and stdev
        # that doesn't change image dimensions
        self.mean_layer = nn.Sequential(nn.Conv2d(reversed_architecture[-1]['out_dim'],
                                                  out_channels=observation_space.shape[0], # TODO assumes channels are 0th dim?
                                                  kernel_size=3,
                                                  padding=1)) # This isn't always positive; is that okay?
        if self.learn_scale:
            self.std_layer = nn.Sequential(nn.Conv2d(reversed_architecture[-1]['out_dim'],
                                                      out_channels=observation_space.shape[0], # TODO assumes channels are 0th dim?
                                                      kernel_size=3,
                                                      padding=1))

    def decode_context(self, z_dist, traj_info, extra_context=None):
        z = self.get_vector(z_dist)
        batch_dim = z.shape[0]

        # Project z to have the number of dimensions needed to reshape into (channels, shape, shape)
        if self.action_representation_dim is not None:
            action_representation = self.get_vector(extra_context)
            logging.debug(f"Action Representation shape: {action_representation.shape}")
            projected_z = self.initial_layer(torch.cat([z, action_representation], dim=1))
        else:
            projected_z = self.initial_layer(z)

        # Do the reshaping
        reshaped_z = projected_z.view([batch_dim] + list(self.full_input_shape))
        # Decode latents into a pixel shape
        decoded_latents = self.decoder(reshaped_z)

        # Calculate final mean and std dev of decoded pixels
        mean_pixels = self.mean_layer(decoded_latents)
        if self.learn_scale:
            std_pixels = torch.exp(self.std_layer(decoded_latents))
        else:
            std_pixels = torch.full(size=mean_pixels.shape, fill_value=self.constant_stddev)
        return independent_multivariate_normal(mean=mean_pixels,
                                               stddev=std_pixels)

    def decode_target(self, z_dist, traj_info, extra_context=None):
        return z_dist


class ActionConditionedVectorDecoder(LossDecoder):
    """
    A decoder that concatenates the frame representation
    and the action representation, and predicts a vector from it
    for use in contrastive losses
    """
    def __init__(self, representation_dim, projection_shape, sample=False, action_representation_dim=128,
                 learn_scale=False):
        super(ActionConditionedVectorDecoder, self).__init__(representation_dim, projection_shape, sample=sample)
        self.learn_scale = learn_scale

        # Machinery for mapping a concatenated (context representation, action representation) into a projection
        self.action_conditioned_projection = nn.Linear(representation_dim + action_representation_dim, projection_shape)

        # If learning scale/std deviation parameter, declare a layer for that, otherwise, return a unit-constant vector
        if self.learn_scale:
            self.scale_projection = nn.Linear(representation_dim + action_representation_dim, projection_shape)
        else:
            self.scale_projection = self.ones_like_projection_dim

    def decode_target(self, z_dist, traj_info, extra_context=None):
        return z_dist

    def decode_context(self, z_dist, traj_info, extra_context=None):
        # Get a single vector out of the the distribution object passed in by the
        # encoder (either via sampling or taking the mean)
        z = self.get_vector(z_dist)
        action_encoding_vector = self.get_vector(extra_context)
        merged_vector = torch.cat([z, action_encoding_vector], dim=1)
        mean_projection = self.action_conditioned_projection(merged_vector)
        scale = self.scale_projection(merged_vector)
        return independent_multivariate_normal(mean=mean_projection,
                                               stddev=scale)

