# -*- coding: utf-8 -*-
"""PRIVATE nshepperd's JAX CLIP Guided Diffusion v2

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1QlSbewwrbRb3gg8C2LBOcLOoIOx4z6Fa

# Generates images from text prompts with CLIP guided diffusion.

Based on my previous jax port of Katherine Crowson's CLIP guided diffusion notebook.
 - [nshepperd's JAX CLIP Guided Diffusion 512x512.ipynb](https://colab.research.google.com/drive/1ZZi1djM8lU4sorkve3bD6EBHiHs6uNAi)
 - [CLIP Guided Diffusion HQ 512x512.ipynb](https://colab.research.google.com/drive/1V66mUeJbXrTuQITvJunvnWVn96FEbSI3)

Added multi-perceptor and pytree ~trickery~ while eliminating the complicated OpenAI gaussian_diffusion classes. Supports both 256x256 and 512x512 OpenAI models (just change the `'image_size': 256` under Model Settings).
 - Added small secondary model for clip guidance.
 - Added anti-jpeg model for clearer samples.
 - Added secondary anti-jpeg classifier.
 - Added Katherine Crowson's v diffusion models (https://github.com/crowsonkb/v-diffusion-jax).
 - Added pixel art model.
 - Added cc12m_1 model (https://github.com/crowsonkb/v-diffusion-pytorch)
 - Reparameterized in terms of cosine t, to allow different schedules; added spliced ddpm+cosine schedule.
"""

#@title Licensed under the MIT License { display-mode: "form" }

# Copyright (c) 2021 Katherine Crowson; nshepperd

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

# !nvidia-smi

# # Workaround for https://github.com/googlecolab/colabtools/issues/2452
# !nvidia-smi | grep A100 && pip install https://storage.googleapis.com/jax-releases/cuda111/jaxlib-0.1.71+cuda111-cp37-none-manylinux2010_x86_64.whl

# # Install dependencies
# #!pip install tensorflow==1.15.2
# !pip install dm-haiku cbor2 ftfy einops braceexpand
# !git clone https://github.com/kingoflolz/CLIP_JAX
# !git clone https://github.com/nshepperd/jax-guided-diffusion -b v2
# !git clone https://github.com/crowsonkb/v-diffusion-jax

import sys, os
sys.path.append('./CLIP_JAX')
sys.path.append('./v-diffusion-jax')
os.environ['XLA_PYTHON_CLIENT_ALLOCATOR'] = 'platform'

from PIL import Image
from braceexpand import braceexpand
from dataclasses import dataclass
from functools import partial
from subprocess import Popen, PIPE
import functools
import io
import math
import re
import requests
import time

import numpy as np
import jax
import jax.numpy as jnp
import jax.scipy as jsp
import jaxtorch
from jaxtorch import PRNG, Context, Module, nn, init
from tqdm import tqdm

from IPython import display
from torchvision import datasets, transforms, utils
from torchvision.transforms import functional as TF
import torch.utils.data
import torch

from diffusion_models.common import DiffusionOutput, Partial, make_partial, blur_fft, norm1, LerpModels
from diffusion_models.lazy import LazyParams
from diffusion_models.schedules import cosine, ddpm, ddpm2, spliced
from diffusion_models.perceptor import get_clip, clip_size, normalize

from diffusion_models.aesthetic import AestheticLoss, AestheticExpected
from diffusion_models.secondary import secondary1_wrap, secondary2_wrap
from diffusion_models.antijpeg import anti_jpeg_cfg, jpeg_classifier
from diffusion_models.pixelart import pixelartv4_wrap, pixelartv6_wrap
from diffusion_models.pixelartv7 import pixelartv7_ic_attn
from diffusion_models.cc12m_1 import cc12m_1_cfg_wrap, cc12m_1_classifier_wrap
from diffusion_models.openai import openai_256, openai_512, openai_512_finetune
from diffusion_models.kat_models import danbooru_128, wikiart_128, wikiart_256, imagenet_128
from diffusion_models import sampler, adapter

devices = jax.devices()
n_devices = len(devices)
print('Using device:', devices)

# Mount drive for saving samples and caching model parameters
MOUNT_DRIVE=False

if MOUNT_DRIVE:
  from google.colab import drive
  drive.mount('/content/drive')
  save_location = '/content/drive/MyDrive/samples/v2'
  model_location = '/content/drive/MyDrive/models'
  os.makedirs(save_location, exist_ok=True)
else:
  save_location = None
  model_location = 'models'

os.makedirs(model_location, exist_ok=True)

# Define necessary functions

def fetch(url_or_path):
    if str(url_or_path).startswith('http://') or str(url_or_path).startswith('https://'):
        r = requests.get(url_or_path)
        r.raise_for_status()
        fd = io.BytesIO()
        fd.write(r.content)
        fd.seek(0)
        return fd
    return open(url_or_path, 'rb')

def fetch_model(url_or_path):
    basename = os.path.basename(url_or_path)
    local_path = os.path.join(model_location, basename)
    if os.path.exists(url_or_path):
      return url_or_path
    elif os.path.exists(local_path):
      return local_path
    elif url_or_path.startswith('http'):
        os.makedirs(f'{model_location}/tmp', exist_ok=True)
        Popen(['curl', url_or_path, '-o', f'{model_location}/tmp/{basename}']).wait()
        os.rename(f'{model_location}/tmp/{basename}', local_path)
        return local_path
    elif url_or_path.startswith('gs://'):
        os.makedirs(f'{model_location}/tmp', exist_ok=True)
        Popen(['gsutil', 'cp', url_or_path, f'{model_location}/tmp/{basename}']).wait()
        os.rename(f'{model_location}/tmp/{basename}', local_path)
        return local_path
LazyParams.fetch = fetch_model

def grey(image):
    [*_, c, h, w] = image.shape
    return jnp.broadcast_to(image.mean(axis=-3, keepdims=True), image.shape)

def cutout_image(image, offsetx, offsety, size, output_size=224):
    """Computes (square) cutouts of an image given x and y offsets and size."""
    (c, h, w) = image.shape

    scale = jnp.stack([output_size / size, output_size / size])
    translation = jnp.stack([-offsety * output_size / size, -offsetx * output_size / size])
    return jax.image.scale_and_translate(image,
                                         shape=(c, output_size, output_size),
                                         spatial_dims=(1,2),
                                         scale=scale,
                                         translation=translation,
                                         method='lanczos3')

def cutouts_images(image, offsetx, offsety, size, output_size=224):
    f = partial(cutout_image, output_size=output_size)         # [c h w] [] [] [] -> [c h w]
    f = jax.vmap(f, in_axes=(0, 0, 0, 0), out_axes=0)          # [n c h w] [n] [n] [n] -> [n c h w]
    f = jax.vmap(f, in_axes=(None, 0, 0, 0), out_axes=0)       # [n c h w] [k n] [k n] [k n] -> [k n c h w]
    return f(image, offsetx, offsety, size)

@jax.tree_util.register_pytree_node_class
class MakeCutouts(object):
    def __init__(self, cut_size, cutn, cut_pow=1.0, p_grey=0.2, p_mixgrey=None, p_flip=0.5):
        self.cut_size = cut_size
        self.cutn = cutn
        self.cut_pow = cut_pow
        self.p_grey = p_grey
        self.p_mixgrey = p_mixgrey
        self.p_flip = p_flip

    def __call__(self, input, key):
        [n, c, h, w] = input.shape
        rng = PRNG(key)

        small_cuts = self.cutn//2
        large_cuts = self.cutn - self.cutn//2

        max_size = min(h, w)
        min_size = min(h, w, self.cut_size)
        cut_us = jax.random.uniform(rng.split(), shape=[small_cuts, n])**self.cut_pow
        sizes = (min_size + cut_us * (max_size - min_size)).clamp(min_size, max_size)
        offsets_x = jax.random.uniform(rng.split(), [small_cuts, n], minval=0, maxval=w - sizes)
        offsets_y = jax.random.uniform(rng.split(), [small_cuts, n], minval=0, maxval=h - sizes)
        cutouts = cutouts_images(input, offsets_x, offsets_y, sizes)

        B1 = 40
        B2 = 40
        lcut_us = jax.random.uniform(rng.split(), shape=[large_cuts, n])
        border = B1 + lcut_us * B2
        lsizes = (max(h,w) + border).astype(jnp.int32)
        loffsets_x = jax.random.uniform(rng.split(), [large_cuts, n], minval=w/2-lsizes/2-border, maxval=w/2-lsizes/2+border)
        loffsets_y = jax.random.uniform(rng.split(), [large_cuts, n], minval=h/2-lsizes/2-border, maxval=h/2-lsizes/2+border)
        lcutouts = cutouts_images(input, loffsets_x, loffsets_y, lsizes)

        cutouts = jnp.concatenate([cutouts, lcutouts], axis=0)

        greyed = grey(cutouts)

        if self.p_mixgrey is not None:
          grey_us = jax.random.uniform(rng.split(), shape=[self.cutn, n, 1, 1, 1])
          grey_rs = jax.random.uniform(rng.split(), shape=[self.cutn, n, 1, 1, 1])
          cutouts = jnp.where(grey_us < self.p_mixgrey, grey_rs * greyed + (1 - grey_rs) * cutouts, cutouts)

        if self.p_grey is not None:
          grey_us = jax.random.uniform(rng.split(), shape=[self.cutn, n, 1, 1, 1])
          cutouts = jnp.where(grey_us < self.p_grey, greyed, cutouts)

        if self.p_flip is not None:
          flip_us = jax.random.bernoulli(rng.split(), self.p_flip, [self.cutn, n, 1, 1, 1])
          cutouts = jnp.where(flip_us, jnp.flip(cutouts, axis=-1), cutouts)

        return cutouts

    def tree_flatten(self):
        return ([self.cut_pow, self.p_grey, self.p_mixgrey, self.p_flip], (self.cut_size, self.cutn))

    @staticmethod
    def tree_unflatten(static, dynamic):
        (cut_size, cutn) = static
        return MakeCutouts(cut_size, cutn, *dynamic)

@jax.tree_util.register_pytree_node_class
class MakeCutoutsPixelated(object):
    def __init__(self, make_cutouts, factor=4):
        self.make_cutouts = make_cutouts
        self.factor = factor
        self.cutn = make_cutouts.cutn

    def __call__(self, input, key):
        [n, c, h, w] = input.shape
        input = jax.image.resize(input, [n, c, h*self.factor, w * self.factor], method='nearest')
        return self.make_cutouts(input, key)

    def tree_flatten(self):
        return ([self.make_cutouts], [self.factor])
    @staticmethod
    def tree_unflatten(static, dynamic):
        return MakeCutoutsPixelated(*dynamic, *static)

def spherical_dist_loss(x, y):
    x = norm1(x)
    y = norm1(y)
    return (x - y).square().sum(axis=-1).sqrt().div(2).arcsin().square().mul(2)


# Define combinators.

# These (ab)use the jax pytree registration system to define parameterised
# objects for doing various things, which are compatible with jax.jit.

# For jit compatibility an object needs to act as a pytree, which means implementing two methods:
#  - tree_flatten(self): returns two lists of the object's fields:
#       1. 'dynamic' parameters: things which can be jax tensors, or other pytrees
#       2. 'static' parameters: arbitrary python objects, will trigger recompilation when changed
#  - tree_unflatten(static, dynamic): reconstitutes the object from its parts

# With these tricks, you can simply define your cond_fn as an object, as is done
# below, and pass it into the jitted sample step as a regular argument. JAX will
# handle recompiling the jitted code whenever a control-flow affecting parameter
# is changed (such as cut_batches).

# A wrapper that causes the diffusion model to generate tileable images, by
# randomly shifting the image with wrap around.

def xyroll(x, shifts):
  return jax.vmap(partial(jnp.roll, axis=[1,2]), in_axes=(0, 0))(x, shifts)

@make_partial
def TilingModel(model, x, cosine_t, key):
  rng = PRNG(key)
  [n, c, h, w] = x.shape
  shift = jax.random.randint(rng.split(), [n, 2], -50, 50)
  x = xyroll(x, shift)
  out = model(x, cosine_t, rng.split())
  def unshift(val):
    return xyroll(val, -shift)
  return jax.tree_util.tree_map(unshift, out)

@make_partial
def PanoramaModel(model, x, cosine_t, key):
  rng = PRNG(key)
  [n, c, h, w] = x.shape
  shift = jax.random.randint(rng.split(), [n, 2], 0, [1, w])
  x = xyroll(x, shift)
  out = model(x, cosine_t, rng.split())
  def unshift(val):
    return xyroll(val, -shift)
  return jax.tree_util.tree_map(unshift, out)

# Models and parameters

# Pixel art model
# There are many checkpoints supported with this model, so maybe better to provide choice in the notebook
pixelartv4_params = LazyParams.pt(
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-v4_34.pt'
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-v4_63.pt'
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-v4_150.pt'
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-v5_50.pt'
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-v5_65.pt'
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-v5_97.pt'
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-v5_173.pt'
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-fgood_344.pt'
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-fgood_432.pt'
    'https://set.zlkj.in/models/diffusion/pixelart/pixelart-fgood_600.pt'
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-fgood_700.pt'
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-fgood_800.pt'
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-fgood_1000.pt'
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-fgood_2000.pt'
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-fgood_3000.pt'
    , key='params_ema'
)

pixelartv6_params = LazyParams.pt(
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-v6-1000.pt'
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-v6-2000.pt'
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-v6-3000.pt'
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-v6-4000.pt'
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-v6-aug-900.pt'
    # 'https://set.zlkj.in/models/diffusion/pixelart/pixelart-v6-aug-1300.pt'
    'https://set.zlkj.in/models/diffusion/pixelart/pixelart-v6-aug-3000.pt'
    , key='params_ema'
)

# Losses and cond fn.

def filternone(xs):
  return [x for x in xs if x is not None]

@jax.tree_util.register_pytree_node_class
class CondCLIP(object):
    """Backward a loss function through clip."""
    def __init__(self, perceptor, make_cutouts, cut_batches, *losses):
        self.perceptor = perceptor
        self.make_cutouts = make_cutouts
        self.cut_batches = cut_batches
        self.losses = filternone(losses)
    def __call__(self, x_in, key):
        n = x_in.shape[0]
        def main_clip_loss(x_in, key):
            cutouts = normalize(self.make_cutouts(x_in.add(1).div(2), key)).rearrange('k n c h w -> (k n) c h w')
            image_embeds = self.perceptor.embed_cutouts(cutouts).rearrange('(k n) c -> k n c', n=n)
            return sum(loss_fn(image_embeds) for loss_fn in self.losses)
        num_cuts = self.cut_batches
        keys = jnp.stack(jax.random.split(key, num_cuts))
        main_clip_grad = jax.lax.scan(lambda total, key: (total + jax.grad(main_clip_loss)(x_in, key), key),
                                        jnp.zeros_like(x_in),
                                        keys)[0] / num_cuts
        return main_clip_grad
    def tree_flatten(self):
        return [self.perceptor, self.make_cutouts, self.losses], [self.cut_batches]
    @classmethod
    def tree_unflatten(cls, static, dynamic):
        [perceptor, make_cutouts, losses] = dynamic
        [cut_batches] = static
        return cls(perceptor, make_cutouts, cut_batches, *losses)

@make_partial
def SphericalDistLoss(text_embed, clip_guidance_scale, image_embeds):
    losses = spherical_dist_loss(image_embeds, text_embed).mean(0)
    return (clip_guidance_scale * losses).sum()

@make_partial
def InfoLOOB(text_embed, clip_guidance_scale, inv_tau, lm, image_embeds):
    all_image_embeds = norm1(image_embeds.mean(0))
    all_text_embeds = norm1(text_embed)
    sim_matrix = inv_tau * jnp.einsum('nc,mc->nm', all_image_embeds, all_text_embeds)
    xn = sim_matrix.shape[0]
    def loob(sim_matrix):
      diag = jnp.eye(xn) * sim_matrix
      off_diag = (1 - jnp.eye(xn))*sim_matrix + jnp.eye(xn) * float('-inf')
      return -diag.sum() + lm * jsp.special.logsumexp(off_diag, axis=-1).sum()
    losses = loob(sim_matrix) + loob(sim_matrix.transpose())
    return losses.sum() * clip_guidance_scale.mean() / inv_tau

@make_partial
def CondTV(tv_scale, x_in, key):
    def downscale2d(image, f):
        [c, n, h, w] = image.shape
        return jax.image.resize(image, [c, n, h//f, w//f], method='cubic')

    def tv_loss(input):
        """L2 total variation loss, as in Mahendran et al."""
        x_diff = input[..., :, 1:] - input[..., :, :-1]
        y_diff = input[..., 1:, :] - input[..., :-1, :]
        return x_diff.square().mean([1,2,3]) + y_diff.square().mean([1,2,3])

    def sum_tv_loss(x_in, f=None):
        if f is not None:
            x_in = downscale2d(x_in, f)
        return tv_loss(x_in).sum() * tv_scale
    tv_grad_512 = jax.grad(sum_tv_loss)(x_in)
    tv_grad_256 = jax.grad(partial(sum_tv_loss,f=2))(x_in)
    tv_grad_128 = jax.grad(partial(sum_tv_loss,f=4))(x_in)
    return tv_grad_512 + tv_grad_256 + tv_grad_128

@make_partial
def CondRange(range_scale, x_in, key):
    def range_loss(x_in):
        return jnp.abs(x_in - x_in.clamp(minval=-1,maxval=1)).mean()
    return range_scale * jax.grad(saturation_loss)(x_in)

@make_partial
def CondMSE(target, mse_scale, x_in, key):
    def mse_loss(x_in):
        return (x_in - target).square().mean()
    return mse_scale * jax.grad(mse_loss)(x_in)

@jax.tree_util.register_pytree_node_class
class MaskedMSE(object):
    # MSE loss. Targets the output towards an image.
    def __init__(self, target, mse_scale, mask, grey=False):
        self.target = target
        self.mse_scale = mse_scale
        self.mask = mask
        self.grey = grey
    def __call__(self, x_in, key):
        def mse_loss(x_in):
            if self.grey:
              return (self.mask * grey(x_in - self.target).square()).mean()
            else:
              return (self.mask * (x_in - self.target).square()).mean()
        return self.mse_scale * jax.grad(mse_loss)(x_in)
    def tree_flatten(self):
        return [self.target, self.mse_scale, self.mask], [self.grey]
    def tree_unflatten(static, dynamic):
        return MaskedMSE(*dynamic, *static)


@jax.tree_util.register_pytree_node_class
class MainCondFn(object):
    # Used to construct the main cond_fn. Accepts a diffusion model which will
    # be used for denoising, plus a list of 'conditions' which will
    # generate gradient of a loss wrt the denoised, to be summed together.
    def __init__(self, diffusion, conditions, blur_amount=None, use='pred'):
        self.diffusion = diffusion
        self.conditions = [c for c in conditions if c is not None]
        self.blur_amount = blur_amount
        self.use = use

    @jax.jit
    def __call__(self, x, cosine_t, key):
        if not self.conditions:
          return jnp.zeros_like(x)

        rng = PRNG(key)
        n = x.shape[0]

        alphas, sigmas = cosine.to_alpha_sigma(cosine_t)

        def denoise(key, x):
            pred = self.diffusion(x, cosine_t, key).pred
            if self.use == 'pred':
                return pred
            elif self.use == 'x_in':
                return pred * sigmas + x * alphas
        (x_in, backward) = jax.vjp(partial(denoise, rng.split()), x)

        total = jnp.zeros_like(x_in)
        for cond in self.conditions:
            total += cond(x_in, rng.split())
        if self.blur_amount is not None:
          blur_radius = (self.blur_amount * sigmas / alphas).clamp(0.05,512)
          total = blur_fft(total, blur_radius.mean())
        final_grad = -backward(total)[0]

        # clamp gradients to a max of 0.2
        magnitude = final_grad.square().mean(axis=(1,2,3), keepdims=True).sqrt()
        final_grad = final_grad * jnp.where(magnitude > 0.2, 0.2 / magnitude, 1.0)
        return final_grad
    def tree_flatten(self):
        return [self.diffusion, self.conditions, self.blur_amount], [self.use]
    def tree_unflatten(static, dynamic):
        return MainCondFn(*dynamic, *static)


@jax.tree_util.register_pytree_node_class
class CondFns(object):
    def __init__(self, *conditions):
        self.conditions = conditions
    def __call__(self, x, t, key):
        rng = PRNG(key)
        total = jnp.zeros_like(x)
        for cond in self.conditions:
          total += cond(x, t, key)
        return total
    def tree_flatten(self):
        return [self.conditions], []
    def tree_unflatten(static, dynamic):
        [conditions] = dynamic
        return CondFns(*conditions)

def clamp_score(score):
  magnitude = score.square().mean(axis=(1,2,3), keepdims=True).sqrt()
  return score * jnp.where(magnitude > 0.1, 0.1 / magnitude, 1.0)


@make_partial
def BlurRangeLoss(scale, x, cosine_t, key):
    def blurred_pred(x, cosine_t):
      alpha, sigma = cosine.to_alpha_sigma(cosine_t)
      blur_radius = (sigma / alpha * 2)
      return blur_fft(x, blur_radius) / alpha.clamp(0.01)
    def loss(x):
        pred = blurred_pred(x, cosine_t)
        diff = pred - pred.clamp(minval=-1,maxval=1)
        return diff.square().sum()
    return clamp_score(-scale * jax.grad(loss)(x))

def process_prompt(clip, prompt):
  expands = braceexpand(prompt)
  embeds = []
  for sub in expands:
    mult = 1.0
    if '~' in sub:
      mult *= -1.0
    sub = sub.replace('~', '')
    embeds.append(mult * clip.embed_text(sub))
  return norm1(sum(embeds))

def process_prompts(clip, prompts):
  return jnp.stack([process_prompt(clip, prompt) for prompt in prompts])

def expand(xs, batch_size):
  return (xs * batch_size)[:batch_size]

"""Configuration for the run"""

seed = None # if None, uses the current time in seconds.
image_size = (320,256)
batch_size = 4
n_batches = 1

main_model = 'pixelartv6'
secondary_model = None # None | secondary2

enable_anti_jpeg = False # Useful for openai or cc12m_1_cfg
clips = ['ViT-B/32', 'ViT-B/16'] # 'ViT-L/14'


all_title = '"Memories of what happened leave me little butterflies", by Mili'
title = expand([all_title], batch_size)

# all_title = 'concept art of the mirror dimension by steven belledin #pixelart'
# all_title = 'A surreal landscape, where the sky is covered in flowers, the flowers represent the gods and the mountains are the sacred temple. {unreal engine,trending on artstation}'
# all_title = 'war angel in sanctuary garden, trending on artstation'
# all_title = 'an illuminati conspiracy of punks. a hermetic order of rainbow punks in a secret underground facility. matte painting trending on artstation'
# all_title = 'a holographic TI-84 graphing calculator interface from the year 2077. unreal engine'
# title = [all_title] * batch_size


# For cc12m_1_cfg
cfg_guidance_scale = 12.0

# For aesthetic loss, requires ViT-B/16
aesthetic_loss_scale = 16.0

# For pixelartv7_ic_attn
ic_cond = 'https://irc.zlkj.in/uploads/eebeaf1803e898ac/88552154_p0%20-%20Coral.png'
ic_guidance_scale = 2.0

# 'https://set.zlkj.in/data/openimages/validation-512/0a1f4761dc7fe1eb.png'
# 'https://set.zlkj.in/data/danbooru/val/danbooru2020/512px/0004/1608004.jpg'
# '/home/em/Downloads/starry_night_full.jpg'
# 'https://set.zlkj.in/data/danbooru/val/danbooru2020/original/0039/1873039.jpg'
# 'https://irc.zlkj.in/uploads/eebeaf1803e898ac/88552154_p0%20-%20Coral.png'
# 'https://cdn.discordapp.com/emojis/916943952597360690.png?size=240&quality=lossless' # pizagal

clip_guidance_scale = 2000.0 # Note: with two perceptors, effective guidance scale is ~2x because they are added together.
tv_scale = 0  # Smooths out the image
sat_scale = 0 # Tries to prevent pixel values from going out of range

cutn = 8        # Effective cutn is cut_batches * this
cut_batches = 4
cut_pow = 1.0   # Affects the size of cutouts. Larger cut_pow -> smaller cutouts (down to the min of 224x244)
cut_p_mixgrey = None # 0.5
cut_p_grey = 0.2
cut_p_flip = 0.5
make_cutouts = MakeCutoutsPixelated(MakeCutouts(clip_size, cutn, cut_pow=cut_pow, p_grey=cut_p_grey, p_flip=cut_p_grey, p_mixgrey=cut_p_mixgrey))

# sample_mode:
#  prk : high quality, 3x slow (eta=0)
#  plms : high quality, about as fast as ddim (eta=0)
#  ddim : traditional, accepts eta for different noise levels which sometimes have nice aesthetic effect
sample_mode = 'ddim'

steps = 250     # Number of steps for sampling. Generally, more = better.
eta = 1.0       # Only applies to ddim sample loop: 0.0: DDIM | 1.0: DDPM | -1.0: Extreme noise (q_sample)
starting_noise = 1.0   # Between 0 and 1. When using init image, generally 0.5-0.8 is good. Lower starting noise makes the result look more like the init.
ending_noise = 0.0     # Usually 0.0 for high detail. Can set a little higher like 0.05 for smoother looking result.

init_image = None      # Diffusion will start with a mixture of this image with noise.
init_weight_mse = 0    # MSE loss between the output and the init makes the result look more like the init (should be between 0 and width*height*3).

# OpenAI used T=1000 to 0. We've just rescaled to between 1 and 0.
schedule = jnp.linspace(starting_noise, ending_noise, steps+1)
schedule = spliced.to_cosine(schedule)

def load_image(url):
    init_array = Image.open(fetch(url)).convert('RGB')
    init_array = init_array.resize(image_size, Image.LANCZOS)
    init_array = jnp.array(TF.to_tensor(init_array)).unsqueeze(0).mul(2).sub(1)
    return init_array

if type(init_image) is list:
    init_array = sum(load_image(url) for url in init_image) / len(init_image)
elif type(init_image) is str:
    init_array = jnp.concatenate([load_image(it) for it in braceexpand(init_image)], axis=0)
else:
    init_array = None

def config():
    vitb32 = lambda: get_clip('ViT-B/32')
    vitb16 = lambda: get_clip('ViT-B/16')
    vitl14 = lambda: get_clip('ViT-L/14')

    if main_model == 'openai':
      diffusion = openai_512()
    elif main_model in ('wikiart_256', 'wikiart_128', 'danbooru_128', 'imagenet_128'):
      if main_model == 'wikiart_256':
          diffusion = wikiart_256()
      elif main_model == 'wikiart_128':
          diffusion = wikiart_128()
      elif main_model == 'danbooru_128':
          diffusion = danbooru_128()
      elif main_model == 'imagenet_128':
          diffusion = imagenet_128()
    elif 'pixelart' in main_model:
      # -- pixel art model --
      if main_model == 'pixelartv7_ic_attn':
          cond = jnp.array(TF.to_tensor(Image.open(fetch(ic_cond)).convert('RGB'))) * 2 - 1
          cond = jnp.concatenate([cond]*(image_size[1]//cond.shape[-2]+1), axis=-2)[:, :image_size[1], :]
          cond = jnp.concatenate([cond]*(image_size[0]//cond.shape[-1]+1), axis=-1)[:, :, :image_size[0]]
          cond = cond.broadcast_to([batch_size, 3, image_size[1], image_size[0]])
          diffusion = pixelartv7_ic_attn(cond, ic_guidance_scale)
      elif main_model == 'pixelartv6':
          diffusion = pixelartv6_wrap(pixelartv6_params())
      elif main_model == 'pixelartv4':
          diffusion = pixelartv4_wrap(pixelartv4_params())
    elif main_model == 'cc12m_1_cfg':
      diffusion = cc12m_1_cfg_wrap(clip_embed=vitb16().embed_texts(title), cfg_guidance_scale=cfg_guidance_scale)
    elif main_model == 'openai_finetune':
        diffusion = openai_512_finetune()

    if secondary_model == 'secondary2':
      cond_model = secondary2_wrap()
    else:
      cond_model = diffusion

    if enable_anti_jpeg:
      diffusion = LerpModels([(diffusion, 1.0),
                              (anti_jpeg_cfg(), 1.0)])

    cond_fn = MainCondFn(cond_model, [
      CondCLIP(vitb32(), make_cutouts, cut_batches,
               SphericalDistLoss(process_prompts(vitb32(), title), clip_guidance_scale) if clip_guidance_scale > 0 else None)
      if 'ViT-B/32' in clips and clip_guidance_scale > 0 else None,

      CondCLIP(vitb16(), make_cutouts, cut_batches,
               SphericalDistLoss(process_prompts(vitb16(), title), clip_guidance_scale) if clip_guidance_scale > 0 else None,
               AestheticExpected(aesthetic_loss_scale) if aesthetic_loss_scale > 0 else None)
      if 'ViT-B/16' in clips and (clip_guidance_scale > 0 or aesthetic_loss_scale > 0) else None,

      CondCLIP(vitl14(), make_cutouts, cut_batches,
               SphericalDistLoss(process_prompts(vitl14(), title), clip_guidance_scale) if clip_guidance_scale > 0 else None)
      if 'ViT-L/14' in clips and clip_guidance_scale > 0 else None,

      CondTV(tv_scale) if tv_scale > 0 else None,
      CondMSE(init_array, init_weight_mse) if init_weight_mse > 0 else None,
      CondSat(sat_scale) if sat_scale > 0 else None,
    ])

    return diffusion, cond_fn

diffusion, cond_fn = config()

# Actually do the run

def sanitize(title):
  return title[:100].replace('/', '_').replace('\\', '_')

@torch.no_grad()
def run():
    if seed is None:
        local_seed = int(time.time())
    else:
        local_seed = seed
    print(f'Starting run with seed {local_seed}...')
    rng = PRNG(jax.random.PRNGKey(local_seed))

    for i in range(n_batches):
        timestring = time.strftime('%Y%m%d%H%M%S')

        ts = schedule
        alphas, sigmas = cosine.to_alpha_sigma(ts)

        print(ts[0], sigmas[0], alphas[0])

        x = jax.random.normal(rng.split(), [batch_size, 3, image_size[1], image_size[0]])

        if init_array is not None:
            x = sigmas[0] * x + alphas[0] * init_array

        grid_width = 2

        # Main loop
        [n, c, h, w] = x.shape
        if sample_mode == 'ddim':
          sample_loop = partial(sampler.ddim_sample_loop, eta=eta)
        elif sample_mode == 'prk':
          sample_loop = sampler.prk_sample_loop
        elif sample_mode == 'plms':
          sample_loop = sampler.plms_sample_loop
        for output in sampler.ddim_sample_loop(diffusion, cond_fn, x, schedule, rng.split()):
            j = output['step']
            pred = output['pred']
            # == Panorama ==
            # shift = jax.random.randint(rng.split(), [batch_size, 2], 0, jnp.array([1, image_size[0]]))
            # x = xyroll(x, shift)
            # == -------- ==
            # diffusion.set(clip_embed=jax.random.normal(rng.split(), [batch_size,512]))
            assert x.isfinite().all().item()
            if j % 10 == 0 or j == steps:
                images = pred
                # images = jnp.concatenate([images, x], axis=0)
                images = images.add(1).div(2).clamp(0, 1)
                images = torch.tensor(np.array(images))
                TF.to_pil_image(utils.make_grid(images, grid_width).cpu()).save(f'progress_{j:05}.png')

        # Save samples
        os.makedirs('samples/grid', exist_ok=True)
        if save_location:
          os.makedirs(f'{save_location}/grid', exist_ok=True)
        TF.to_pil_image(utils.make_grid(images, grid_width).cpu()).save(f'samples/grid/{timestring}_{sanitize(all_title)}.png')
        if save_location:
            TF.to_pil_image(utils.make_grid(images, grid_width).cpu()).save(f'{save_location}/grid/{timestring}_{sanitize(all_title)}.png')

        os.makedirs('samples/images', exist_ok=True)
        if save_location:
          os.makedirs(f'{save_location}/images', exist_ok=True)
        for k in range(batch_size):
            this_title = sanitize(title[k])
            dname = f'samples/images/{timestring}_{k}_{this_title}.png'
            pil_image = TF.to_pil_image(images[k])
            pil_image.save(dname)
            if save_location:
              pil_image.save(f'{save_location}/images/{timestring}_{k}_{this_title}.png')

try:
  run()
  success = True
except:
  import traceback
  traceback.print_exc()
  success = False
assert success