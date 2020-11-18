"""Code for automatically loading data, creating vecenvs, etc. based on
Sacred configuration."""

import logging

from imitation.util.util import make_vec_env
from stable_baselines3.common.atari_wrappers import AtariWrapper
from stable_baselines3.common.vec_env import VecFrameStack, VecTransposeImage

from il_representations.algos.augmenters import ColorSpace
from il_representations.envs.atari_envs import load_dataset_atari
from il_representations.envs.config import (env_cfg_ingredient,
                                            venv_opts_ingredient)
from il_representations.envs.dm_control_envs import load_dataset_dm_control
from il_representations.envs.magical_envs import (get_env_name_magical,
                                                  load_dataset_magical)

ERROR_MESSAGE = "no support for benchmark_name={benchmark['benchmark_name']!r}"


@env_cfg_ingredient.capture
def load_dataset(benchmark_name):
    if benchmark_name == 'magical':
        dataset_dict = load_dataset_magical()
    elif benchmark_name == 'dm_control':
        dataset_dict = load_dataset_dm_control()
    elif benchmark_name == 'atari':
        dataset_dict = load_dataset_atari()
    else:
        raise NotImplementedError(ERROR_MESSAGE.format(**locals()))

    num_transitions = len(dataset_dict['dones'].flatten())
    num_dones = dataset_dict['dones'].flatten().sum()
    logging.info(f'Loaded dataset with {num_transitions} transitions. '
                 f'{num_dones} of these transitions have done == True')

    return dataset_dict


@env_cfg_ingredient.capture
def get_gym_env_name(benchmark_name, atari_env_id, dm_control_full_env_names,
                     dm_control_env):
    if benchmark_name == 'magical':
        return get_env_name_magical()
    elif benchmark_name == 'dm_control':
        return dm_control_full_env_names[dm_control_env]
    elif benchmark_name == 'atari':
        return atari_env_id
    raise NotImplementedError(ERROR_MESSAGE.format(**locals()))


@venv_opts_ingredient.capture
def _get_venv_opts(n_envs, venv_parallel):
    # helper to extract options from venv_opts, since we can't have two
    # captures on one function (see Sacred issue #206)
    return n_envs, venv_parallel


@env_cfg_ingredient.capture
def load_vec_env(benchmark_name, atari_env_id, dm_control_full_env_names,
                 dm_control_env, dm_control_frame_stack):
    """Create a vec env for the selected benchmark task and wrap it with any
    necessary wrappers."""
    n_envs, venv_parallel = _get_venv_opts()
    gym_env_name = get_gym_env_name()
    if benchmark_name == 'magical':
        return make_vec_env(gym_env_name,
                            n_envs=n_envs,
                            parallel=venv_parallel)
    elif benchmark_name == 'dm_control':
        raw_dmc_env = make_vec_env(gym_env_name,
                                   n_envs=n_envs,
                                   parallel=venv_parallel)
        final_env = VecFrameStack(raw_dmc_env, n_stack=dm_control_frame_stack)
        dmc_chans = raw_dmc_env.observation_space.shape[0]

        # make sure raw env has 3 channels (should be RGB, IIRC)
        assert dmc_chans == 3

        # make sure stacked env has dmc_chans*frame_stack channels
        expected_shape = (dm_control_frame_stack * dmc_chans, ) \
            + raw_dmc_env.observation_space.shape[1:]
        assert final_env.observation_space.shape == expected_shape, \
            (final_env.observation_space.shape, expected_shape)

        # make sure images are square
        assert final_env.observation_space.shape[1:] \
            == final_env.observation_space.shape[1:][::-1]

        return final_env
    elif benchmark_name == 'atari':
        raw_atari_env = make_vec_env(gym_env_name,
                                     n_envs=n_envs,
                                     parallel=venv_parallel,
                                     wrapper_class=AtariWrapper)
        final_env = VecFrameStack(VecTransposeImage(raw_atari_env), 4)
        assert final_env.observation_space.shape == (4, 84, 84), \
            final_env.observation_space.shape
        return final_env
    raise NotImplementedError(ERROR_MESSAGE.format(**locals()))


@env_cfg_ingredient.capture
def load_color_space(benchmark_name):
    color_spaces = {
        'magical': ColorSpace.RGB,
        'dm_control': ColorSpace.RGB,
        'atari': ColorSpace.GRAY,
    }
    try:
        return color_spaces[benchmark_name]
    except KeyError:
        raise NotImplementedError(ERROR_MESSAGE.format(**locals()))
