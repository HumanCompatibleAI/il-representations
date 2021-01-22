"""Miscellaneous tools that don't fit elsewhere."""
import collections
import hashlib
import json
import math
import os
import pdb
import pickle
import re
import sys
from collections.abc import Sequence

from PIL import Image
from imitation.augment.color import ColorSpace
from skvideo.io import FFmpegWriter
import torch as th
import torchvision.utils as vutils


class ForkedPdb(pdb.Pdb):
    """A Pdb subclass that may be used
    from a forked multiprocessing child

    """
    def interaction(self, *args, **kwargs):
        _stdin = sys.stdin
        try:
            sys.stdin = open('/dev/stdin')
            pdb.Pdb.interaction(self, *args, **kwargs)
        finally:
            sys.stdin = _stdin


def recursively_sort(element):
    """Ensures that any dicts in nested dict/list object
    collection are converted to OrderedDicts"""
    if isinstance(element, collections.Mapping):
        sorted_dict = collections.OrderedDict()
        for k in sorted(element.keys()):
            sorted_dict[k] = recursively_sort(element[k])
        return sorted_dict
    elif isinstance(element, Sequence) and not isinstance(element, str):
        return [recursively_sort(inner_el) for inner_el in element]
    else:
        return str(element)


def hash_configs(merged_config):
    """MD5 hash of a dictionary."""
    sorted_dict = recursively_sort(merged_config)
    # Needs to be double-encoded because result of jsonpickle is Unicode
    encoded = json.dumps(sorted_dict).encode('utf-8')
    hash = hashlib.md5(encoded).hexdigest()
    return hash


def freeze_params(module):
    """Modifies Torch module in-place to convert all its parameters to buffers,
    and give them require_grad=False. This is a slightly hacky way of
    "freezing" the module."""

    # We maintain this stack so that we can traverse the module tree
    # depth-first. We'll terminate once we've traversed all modules.
    module_stack = [module]

    while module_stack:
        # get next module from end of the stack
        next_module = module_stack.pop()

        # sanity check to ensure we only have named params
        param_list = list(next_module.parameters(recurse=False))
        named_param_list = list(next_module.named_parameters(recurse=False))
        assert len(param_list) == len(named_param_list), \
            f"cannot handle module '{next_module}' with unnamed parameters"

        # now remove each param (delattr) and replace it with a buffer
        # (register_buffer)
        for param_name, param_var in named_param_list:
            param_tensor = param_var.data.clone().detach()
            assert not param_tensor.requires_grad
            delattr(next_module, param_name)
            next_module.register_buffer(param_name, param_tensor)

        # do the same for child modules
        module_stack.extend(next_module.children())

    # sanity check to make sure we have no params on the root module
    remaining_params = list(module.parameters())
    assert len(remaining_params) == 0, \
        f"module '{module}' has params remaining: {remaining_params}"


NUM_CHANS = {
    ColorSpace.RGB: 3,
    ColorSpace.GRAY: 1,
}


def image_tensor_to_rgb_grid(image_tensor, color_space):
    """Converts an image tensor to a montage of images.

    Args:
        image_tensor (Tensor): tensor containing (possibly stacked) frames.
            Tensor values should be in [0, 1], and tensor shape should be […,
            n_frames*chans_per_frame, H, W]; the last three dimensions are
            essential, but the trailing dimensions do not matter.
         color_space (ColorSpace): color space for the images. This is needed
            to infer how many frames are in each frame stack.

    Returns:
         grid (Tensor): a [3*H*W] RGB image containing all the stacked frames
            passed in as input, arranged in a (roughly square) grid.
    """
    assert isinstance(image_tensor, th.Tensor)
    image_tensor = image_tensor.detach().cpu()

    # make sure shape is correct & data is in the right range
    assert image_tensor.ndim >= 3, image_tensor.shape
    assert th.all((-0.01 <= image_tensor) & (image_tensor <= 1.01)), \
        f"this only takes intensity values in [0,1], but range is " \
        f"[{image_tensor.min()}, {image_tensor.max()}]"
    n_chans = NUM_CHANS[color_space]
    assert (image_tensor.shape[-3] % n_chans) == 0, \
        f"expected image to be stack of frames with {n_chans} channels " \
        f"each, but image tensor is of shape {image_tensor.shape}"

    # Reshape into [N,3,H,W] or [N,1,H,W], depending on how many channels there
    # are per frame.
    nchw_tensor = image_tensor.reshape((-1, n_chans) + image_tensor.shape[-2:])

    if n_chans == 1:
        # tile grayscale to RGB
        nchw_tensor = th.cat((nchw_tensor, ) * 3, dim=-3)

    # make sure it really is RGB
    assert nchw_tensor.ndim == 4 and nchw_tensor.shape[1] == 3

    # clamp to right value range
    clamp_tensor = th.clamp(nchw_tensor, 0, 1.)

    # number of rows scales with sqrt(num frames)
    # (this keeps image roughly square)
    nrow = max(1, int(math.sqrt(clamp_tensor.shape[0])))

    # now convert to an image grid
    grid = vutils.make_grid(clamp_tensor,
                            nrow=nrow,
                            normalize=False,
                            scale_each=False,
                            range=(0, 1))
    assert grid.ndim == 3 and grid.shape[0] == 3, grid.shape

    return grid


def save_rgb_tensor(rgb_tensor, file_path):
    """Save an RGB Torch tensor to a file. It is assumed that rgb_tensor is of
    shape [3,H,W] (channels-first), and that it has values in [0,1]."""
    assert isinstance(rgb_tensor, th.Tensor)
    assert rgb_tensor.ndim == 3 and rgb_tensor.shape[0] == 3, rgb_tensor.shape
    detached = rgb_tensor.detach()
    rgb_tensor_255 = (detached.clamp(0, 1) * 255).round()
    chans_last = rgb_tensor_255.permute((1, 2, 0))
    np_array = chans_last.detach().byte().cpu().numpy()
    pil_image = Image.fromarray(np_array)
    dir_path = os.path.dirname(file_path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    pil_image.save(file_path)


class TensorFrameWriter:
    """Writes N*(F*C)*H*W tensor frames to a video file."""
    def __init__(self, out_path, color_space, fps=25, config=None):
        self.out_path = out_path
        self.color_space = color_space
        ffmpeg_out_config = {
            '-r': str(fps),
            '-vcodec': 'libx264',
            '-pix_fmt': 'yuv420p',
        }
        if config is not None:
            ffmpeg_out_config.update(config)
        self.writer = FFmpegWriter(out_path, outputdict=ffmpeg_out_config)

    def add_tensor(self, tensor):
        """Add a tensor of shape [..., C, H, W] representing the frame stacks
        for a single time step. Call this repeatedly for each time step you
        want to add."""
        if self.writer is None:
            raise RuntimeError("Cannot run add_tensor() again after closing!")
        grid = image_tensor_to_rgb_grid(tensor, self.color_space)
        # convert to (H, W, 3) numpy array
        np_grid = grid.numpy().transpose((1, 2, 0))
        byte_grid = (np_grid * 255).round().astype('uint8')
        self.writer.writeFrame(byte_grid)

    def __enter__(self):
        assert self.writer is not None, \
            "cannot __enter__ this again once it is closed"
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        if self.writer is None:
            return
        self.writer.close()
        self.writer = None

    def __del__(self):
        self.close()


class SaneDict(dict):
    # used in SacredUnpickler
    pass


class SaneList(list):
    # used in SacredUnpickler
    pass


class SacredUnpickler(pickle.Unpickler):
    """Unpickler that replaces Sacred's ReadOnlyDict/ReadOnlyList with
    dict/list."""
    overrides = {
        # for some reason we need to replace dict with a custom class, or
        # else we get an AttributeError complaining that 'dict' has no
        # attribute '__dict__' (I don't know why this hapens)
        ('sacred.config.custom_containers', 'ReadOnlyDict'): SaneDict,
        ('sacred.config.custom_containers', 'ReadOnlyList'): SaneList,
    }

    def find_class(self, module, name):
        key = (module, name)
        if key in self.overrides:
            return self.overrides[key]
        return super().find_class(module, name)


def load_sacred_pickle(fp, **kwargs):
    """Unpickle an object that may contain Sacred ReadOnlyDict and ReadOnlyList
    objects. It will convert those objects to plain dicts/lists."""
    return SacredUnpickler(fp, **kwargs).load()


class RepLSaveExampleBatchesCallback:
    """Save (possibly image-based) contexts, targets, and encoded/decoded
    contexts/targets."""
    def __init__(self, save_interval_batches, dest_dir, color_space):
        self.save_interval_batches = save_interval_batches
        self.dest_dir = dest_dir
        self.last_save = None
        self.color_space = color_space

    def __call__(self, repl_locals):
        batches_trained = repl_locals['batches_trained']

        # check whether we should save anything
        should_save = self.last_save is None \
            or self.last_save + self.save_interval_batches <= batches_trained
        if not should_save:
            return
        self.last_save = batches_trained

        os.makedirs(self.dest_dir, exist_ok=True)

        # now loop over items and save using appropriate format
        to_save = [
            'contexts', 'targets', 'extra_context', 'encoded_contexts',
            'encoded_targets', 'encoded_extra_context', 'decoded_contexts',
            'decoded_targets', 'traj_ts_info',
        ]
        for save_name in to_save:
            save_value = repl_locals[save_name]

            if isinstance(save_value, th.distributions.Distribution):
                # take sample instead of mean so that we can see noise
                save_value = save_value.sample()
            if th.is_tensor(save_value):
                save_value = save_value.detach().cpu()

            # heuristic to check if this is an image
            probably_an_image = th.is_tensor(save_value) \
                and save_value.ndim == 4 \
                and save_value.shape[-2] == save_value.shape[-1]
            clean_save_name = re.sub(r'[^\w_ \-]', '-', save_name)
            save_prefix = f'{clean_save_name}_{batches_trained:06d}'
            save_path_no_suffix = os.path.join(self.dest_dir, save_prefix)

            if probably_an_image:
                # probably an image
                save_path = save_path_no_suffix + '.png'
                # save as image
                save_image = save_value.float().clamp(0, 1)
                as_rgb = image_tensor_to_rgb_grid(save_image, self.color_space)
                save_rgb_tensor(as_rgb, save_path)
            else:
                # probably not an image
                save_path = save_path_no_suffix + '.pt'
                # will save with Torch's generic serialisation code
                th.save(save_value, save_path)


class SigmoidRescale(th.nn.Module):
    """Rescales input to be in [min_val, max_val]; useful for pixel decoder."""
    def __init__(self, min_val, max_val):
        super().__init__()
        self.min_val = min_val
        self.val_range = max_val - min_val

    def forward(self, x):
        return th.sigmoid(x) * self.val_range + self.min_val


def up(p):
    """Return the path *above* whatever object the path `p` points to.
    Examples:

        up("/foo/bar") == "/foo"
        up("/foo/bar/") == "/foo
        up(up(up("foo/bar"))) == ".."
    """
    return os.path.normpath(os.path.join(p, ".."))
