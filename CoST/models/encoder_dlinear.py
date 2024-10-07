import math
from typing import List

import torch
from torch import nn
import torch.nn.functional as F
import torch.fft as fft
from einops import reduce, rearrange, repeat

import numpy as np

from .dilated_conv import DilatedConvEncoder


def generate_continuous_mask(B, T, n=5, l=0.1):
    res = torch.full((B, T), True, dtype=torch.bool)
    if isinstance(n, float):
        n = int(n * T)
    n = max(min(n, T // 2), 1)
    
    if isinstance(l, float):
        l = int(l * T)
    l = max(l, 1)
    
    for i in range(B):
        for _ in range(n):
            t = np.random.randint(T-l+1)
            res[i, t:t+l] = False
    return res


def generate_binomial_mask(B, T, p=0.5):
    return torch.from_numpy(np.random.binomial(1, p, size=(B, T))).to(torch.bool)


class BandedFourierLayer(nn.Module):
    def __init__(self, in_channels, out_channels, band, num_bands, length=201):
        super().__init__()

        self.length = length
        self.total_freqs = (self.length // 2) + 1

        self.in_channels = in_channels
        self.out_channels = out_channels

        self.band = band  # zero indexed
        self.num_bands = num_bands

        self.num_freqs = self.total_freqs // self.num_bands + (self.total_freqs % self.num_bands if self.band == self.num_bands - 1 else 0)

        self.start = self.band * (self.total_freqs // self.num_bands)
        self.end = self.start + self.num_freqs


        # case: from other frequencies
        self.weight = nn.Parameter(torch.empty((self.num_freqs, in_channels, out_channels), dtype=torch.cfloat))
        self.bias = nn.Parameter(torch.empty((self.num_freqs, out_channels), dtype=torch.cfloat))
        self.reset_parameters()

    def forward(self, input):
        # input - b t d
        b, t, _ = input.shape
        input_fft = fft.rfft(input, dim=1)
        output_fft = torch.zeros(b, t // 2 + 1, self.out_channels, device=input.device, dtype=torch.cfloat)
        output_fft[:, self.start:self.end] = self._forward(input_fft)
        return fft.irfft(output_fft, n=input.size(1), dim=1)

    def _forward(self, input):
        output = torch.einsum('bti,tio->bto', input[:, self.start:self.end], self.weight)
        return output + self.bias

    def reset_parameters(self) -> None:
        nn.init.kaiming_uniform_(self.weight, a=math.sqrt(5))
        fan_in, _ = nn.init._calculate_fan_in_and_fan_out(self.weight)
        bound = 1 / math.sqrt(fan_in) if fan_in > 0 else 0
        nn.init.uniform_(self.bias, -bound, bound)


class CoSTEncoderDlinear(nn.Module):
    def __init__(self, input_dims, output_dims,
                 kernels: List[int],
                 length: int,
                 hidden_dims=64, depth=10,
                 mask_mode='binomial'):
        super().__init__()

        component_dims = output_dims // 2 # trend and seasonality dimensions

        self.input_dims = input_dims
        self.output_dims = output_dims
        self.component_dims = component_dims
        self.hidden_dims = hidden_dims
        self.mask_mode = mask_mode
        self.input_fc = nn.Linear(input_dims, hidden_dims)

        self.feature_extractor_avg = DilatedConvEncoder(
            hidden_dims,
            [hidden_dims] * depth + [output_dims],
            kernel_size=3
        )

        self.feature_extractor_err = DilatedConvEncoder(
            hidden_dims,
            [hidden_dims] * depth + [output_dims],
            kernel_size=3
        )

        self.repr_dropout = nn.Dropout(p=0.1)

        self.kernels = kernels

        self.tfd = nn.ModuleList(
            [nn.Conv1d(output_dims, component_dims, k, padding=k-1) for k in kernels] # to extract trend
        )

        self.sfd = nn.ModuleList(
            [BandedFourierLayer(output_dims, component_dims, b, 1, length=length) for b in range(1)] #to extract seasonality
        )

    def forward(self, x_avg, x_err, tcn_output=False, mask='all_true'):  # x: B x T x input_dims
        nan_mask = ~x_avg.isnan().any(axis=-1)
        x_avg[~nan_mask] = 0
        x_err[~nan_mask] = 0
        x_avg = self.input_fc(x_avg)  # B x T x Ch
        x_err = self.input_fc(x_err)

        # generate & apply mask
        if mask is None:
            if self.training:
                mask = self.mask_mode
            else:
                mask = 'all_true'

        if mask == 'binomial':
            mask = generate_binomial_mask(x_avg.size(0), x_avg.size(1)).to(x_avg.device)
        elif mask == 'continuous':
            mask = generate_continuous_mask(x_avg.size(0), x_avg.size(1)).to(x_avg.device)
        elif mask == 'all_true':
            mask = x_avg.new_full((x_avg.size(0), x_avg.size(1)), True, dtype=torch.bool)
        elif mask == 'all_false':
            mask = x_avg.new_full((x_avg.size(0), x_avg.size(1)), False, dtype=torch.bool)
        elif mask == 'mask_last':
            mask = x_avg.new_full((x_avg.size(0), x_avg.size(1)), True, dtype=torch.bool)
            mask[:, -1] = False

        mask &= nan_mask
        x_avg[~mask] = 0
        x_err[~mask] = 0

        # conv encoder
        x_avg = x_avg.transpose(1, 2)  # B x Ch x T
        x_avg = self.feature_extractor_avg(x_avg)  # B x Co x T

        x_err = x_err.transpose(1, 2)  # B x Ch x T
        x_err = self.feature_extractor_err(x_err)

        x = x_avg + x_err

        if tcn_output:
            return x_avg.transpose(1, 2)

        trend = []
        for idx, mod in enumerate(self.tfd):
            out = mod(x)  # b d t
            if self.kernels[idx] != 1:
                out = out[..., :-(self.kernels[idx] - 1)]
            trend.append(out.transpose(1, 2))  # b t d
        trend = reduce(
            rearrange(trend, 'list b t d -> list b t d'),
            'list b t d -> b t d', 'mean'
        )

        x = x.transpose(1, 2)  # B x T x Co

        season = []
        for mod in self.sfd:
            out = mod(x)  # b t d
            season.append(out)
        season = season[0]

        return trend, self.repr_dropout(season)
