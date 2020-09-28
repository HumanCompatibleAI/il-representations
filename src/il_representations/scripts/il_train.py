#!/usr/bin/env python3
"""Run an IL algorithm in some selected domain."""
import logging
import os

from imitation.algorithms.adversarial import GAIL
from imitation.algorithms.bc import BC
from imitation.augment import StandardAugmentations
import imitation.util.logger as imitation_logger
import sacred
from sacred import Experiment, Ingredient
from sacred.observers import FileStorageObserver
import stable_baselines3.common.policies as sb3_pols
from stable_baselines3.common.utils import get_device
from stable_baselines3.ppo import PPO
import torch as th
from torch import nn

from il_representations.algos.encoders import DeterministicEncoder
from il_representations.algos.utils import set_global_seeds
from il_representations.data import TransitionsMinimalDataset
import il_representations.envs.auto as auto_env
from il_representations.envs.config import benchmark_ingredient
from il_representations.il.disc_rew_nets import ImageDiscrimNet
from il_representations.policy_interfacing import EncoderFeatureExtractor
from il_representations.utils import freeze_params

bc_ingredient = Ingredient('bc')


@bc_ingredient.config
def bc_defaults():
    # number of passes to make through dataset
    n_epochs = 1000  # noqa: F841
    augs = 'rotate,translate,noise'  # noqa: F841
    log_interval = 500  # noqa: F841


gail_ingredient = Ingredient('gail')


@gail_ingredient.config
def gail_defaults():
    # number of env time steps to perform during reinforcement learning
    total_timesteps = int(1e6)  # noqa: F841
    # "gail_disc_batch_size" is how many samples we take from the expert and
    # novice buffers to do a round of discriminator optimisation.
    # "gail_disc_minibatch_size" controls the size of the minibatches that we
    # divide that into. Thus, we do batch_size/minibatch_size minibatches of
    # optimisation at each discriminator update. gail_disc_batch_size = 256.
    # (this is a different naming convention to SB3 PPO)
    disc_batch_size = 256  # noqa: F841
    disc_minibatch_size = 32  # noqa: F841
    disc_lr = 1e-4  # noqa: F841
    disc_augs = "rotate,translate,noise"  # noqa: F841
    ppo_n_steps = 16  # noqa: F841
    # "batch size" is actually the size of a _minibatch_. The amount of data
    # used for each training update is gail_ppo_n_steps*n_envs.
    ppo_batch_size = 32  # noqa: F841
    ppo_n_epochs = 4  # noqa: F841
    ppo_learning_rate = 2.5e-4  # noqa: F841
    ppo_gamma = 0.95  # noqa: F841
    ppo_gae_lambda = 0.95  # noqa: F841
    ppo_ent = 1e-5  # noqa: F841
    ppo_adv_clip = 0.05  # noqa: F841


sacred.SETTINGS['CAPTURE_MODE'] = 'sys'  # workaround for sacred issue#740
il_train_ex = Experiment('il_train', ingredients=[
    benchmark_ingredient, bc_ingredient, gail_ingredient,
])


@il_train_ex.config
def default_config():
    # random seed for EVERYTHING
    seed = 42  # noqa: F841
    # device to place all computations on
    device_name = 'auto'  # noqa: F841
    # choose between 'bc'/'gail'
    algo = 'bc'  # noqa: F841
    # place to load pretrained encoder from (if not given, it will be
    # re-intialised from scratch)
    encoder_path = None  # noqa: F841
    # file name for final policy
    final_pol_name = 'policy_final.pt'  # noqa: F841
    # should we freeze waits of the encoder?
    freeze_encoder = False  # noqa: F841
    # these defaults are mostly optimised for GAIL, but should be fine for BC
    # too (it only uses the venv for evaluation)
    benchmark = dict(  # noqa: F841
        venv_parallel=True,
        n_envs=16,
    )
    encoder_kwargs = dict(  # noqa: F841
        obs_encoder_cls='BasicCNN',
        representation_dim=128,
    )


@il_train_ex.capture
def make_policy(observation_space, action_space, encoder_or_path, encoder_kwargs, lr_schedule=None):
    # TODO(sam): this should be unified with the representation learning code
    # so that it can be configured in the same way, with the same default
    # encoder architecture & kwargs.
    common_policy_kwargs = {
        'observation_space': observation_space,
        'action_space': action_space,
        # SB3 policies require a learning rate for the embedded optimiser. BC
        # should not use that optimiser, though, so we set the LR to some
        # insane value that is guaranteed to cause problems if the optimiser
        # accidentally is used for something (using infinite or non-numeric
        # values fails initial validation, so we need an insane-but-finite
        # number).
        'lr_schedule': (lambda _: 1e100) if lr_schedule is None else lr_schedule,
        'ortho_init': False,
    }
    if encoder_or_path is not None:
        if isinstance(encoder_or_path, str):
            encoder = th.load(encoder_or_path)
        else:
            encoder = encoder_or_path
        assert isinstance(encoder, nn.Module)
    else:
        encoder = DeterministicEncoder(observation_space, **encoder_kwargs)
    policy_kwargs = {
        'features_extractor_class': EncoderFeatureExtractor,
        'features_extractor_kwargs': {
            "encoder": encoder,
        },
        **common_policy_kwargs,
    }
    policy = sb3_pols.ActorCriticCnnPolicy(**policy_kwargs)
    return policy


@il_train_ex.capture
def do_training_bc(venv_chans_first, dataset, out_dir, bc, encoder,
                   device_name, final_pol_name):
    policy = make_policy(
        observation_space=venv_chans_first.observation_space,
        action_space=venv_chans_first.action_space, encoder_or_path=encoder)
    color_space = auto_env.load_color_space()
    augmenter = StandardAugmentations.from_string_spec(
        bc['augs'], stack_color_space=color_space)
    trainer = BC(
        observation_space=venv_chans_first.observation_space,
        action_space=venv_chans_first.action_space,
        policy_class=lambda **kwargs: policy,
        policy_kwargs=None,
        expert_data=dataset,
        device=device_name,
        augmentation_fn=augmenter,
        optimizer_cls=th.optim.SGD,
        optimizer_kwargs=dict(lr=1e-3, momentum=0.1),
        ent_weight=1e-3,
        l2_weight=1e-5,
    )

    logging.info("Beginning BC training")
    trainer.train(n_epochs=bc['n_epochs'], log_interval=bc['log_interval'])

    final_path = os.path.join(out_dir, final_pol_name)
    logging.info(f"Saving final BC policy to {final_path}")
    trainer.save_policy(final_path)
    return final_path


@il_train_ex.capture
def do_training_gail(
    venv_chans_first,
    dataset,
    device_name,
    encoder,
    out_dir,
    final_pol_name,
    gail,
):
    device = get_device(device_name)
    discrim_net = ImageDiscrimNet(
        observation_space=venv_chans_first.observation_space,
        action_space=venv_chans_first.action_space,
        encoder=encoder,
    )

    def policy_constructor(observation_space, action_space, lr_schedule, use_sde=False):
        """Construct a policy with the right LR schedule (since PPO will
        actually use it, unlike BC)."""
        assert not use_sde
        return make_policy(
            observation_space=observation_space, action_space=action_space,
            encoder_or_path=encoder, lr_schedule=lr_schedule)

    ppo_algo = PPO(
        policy=policy_constructor,
        env=venv_chans_first,
        # verbose=1 and tensorboard_log=False is a hack to work around SB3
        # issue #109.
        verbose=1,
        tensorboard_log=None,
        device=device,
        n_steps=gail['ppo_n_steps'],
        batch_size=gail['ppo_batch_size'],
        n_epochs=gail['ppo_n_epochs'],
        ent_coef=gail['ppo_ent'],
        gamma=gail['ppo_gamma'],
        gae_lambda=gail['ppo_gae_lambda'],
        clip_range=gail['ppo_adv_clip'],
        learning_rate=gail['ppo_learning_rate'],
    )
    color_space = auto_env.load_color_space()
    augmenter = StandardAugmentations.from_string_spec(
        gail['disc_augs'], stack_color_space=color_space)
    trainer = GAIL(
        venv_chans_first,
        dataset,
        ppo_algo,
        disc_batch_size=gail['disc_batch_size'],
        disc_minibatch_size=gail['disc_minibatch_size'],
        discrim_kwargs=dict(discrim_net=discrim_net, scale=True),
        obs_norm=False,
        rew_norm=True,
        disc_opt_kwargs=dict(lr=gail['disc_lr']),
        disc_augmentation_fn=augmenter,
    )

    trainer.train(total_timesteps=gail['total_timesteps'])

    final_path = os.path.join(out_dir, final_pol_name)
    logging.info(f"Saving final GAIL policy to {final_path}")
    th.save(ppo_algo.policy, final_path)
    return final_path


@il_train_ex.main
def train(seed, algo, benchmark, encoder_path, freeze_encoder,
          _config):
    set_global_seeds(seed)
    # python built-in logging
    logging.basicConfig(level=logging.INFO)
    # `imitation` logging
    # FIXME(sam): I used this hack from run_rep_learner.py, but I don't
    # actually know the right way to write log files continuously in Sacred.
    log_dir = os.path.abspath(il_train_ex.observers[0].dir)
    imitation_logger.configure(log_dir, ["stdout", "csv", "tensorboard"])

    venv = auto_env.load_vec_env()
    dataset_dict = auto_env.load_dataset()
    dataset = TransitionsMinimalDataset(dataset_dict)

    if encoder_path:
        logging.info(f"Loading pretrained encoder from '{encoder_path}'")
        encoder = th.load(encoder_path)
        if freeze_encoder:
            freeze_params(encoder)
            assert len(list(encoder.parameters())) == 0
    else:
        logging.info("No encoder provided, will init from scratch")
        encoder = None

    logging.info(f"Setting up '{algo}' IL algorithm")

    if algo == 'bc':
        final_path = do_training_bc(dataset=dataset,
                                    venv_chans_first=venv,
                                    out_dir=log_dir,
                                    encoder=encoder)

    elif algo == 'gail':
        final_path = do_training_gail(dataset=dataset,
                                      venv_chans_first=venv,
                                      out_dir=log_dir,
                                      encoder=encoder)

    else:
        raise NotImplementedError(f"Can't handle algorithm '{algo}'")

    # FIXME(sam): make sure this always closes correctly, even when there's an
    # exception after creating it (could use try/catch or a context manager)
    venv.close()

    return {'model_path': os.path.abspath(final_path)}


if __name__ == '__main__':
    il_train_ex.observers.append(FileStorageObserver('runs/il_train_runs'))
    il_train_ex.run_commandline()
