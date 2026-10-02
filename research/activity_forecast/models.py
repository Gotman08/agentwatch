"""Small forecasting models; graph ablation has exactly the same architecture."""
import numpy as np
import torch
from torch import nn


class TemporalGRU(nn.Module):
    def __init__(self, nodes, hidden, horizon, calls_per_bin):
        super().__init__()
        self.rnn = nn.GRU(nodes, hidden, batch_first=True)
        self.output = nn.Linear(hidden, nodes)
        self.horizon, self.calls_per_bin = horizon, calls_per_bin

    def forward(self, x):
        _, h = self.rnn(x / self.calls_per_bin)
        return self.horizon * self.output(h[-1]).softmax(-1)


class STGNN(nn.Module):
    def __init__(self, graph, hidden, horizon, calls_per_bin):
        super().__init__()
        self.register_buffer('graph', torch.as_tensor(graph).clone().float())
        self.embedding = nn.Parameter(torch.randn(len(graph), 4) * 0.05)
        self.rnn = nn.GRU(6, hidden, batch_first=True)
        self.output = nn.Linear(hidden, 1)
        self.horizon, self.calls_per_bin = horizon, calls_per_bin

    def forward(self, x):
        x = x / self.calls_per_bin
        mixed = torch.einsum('ij,btj->bti', self.graph, x)
        signal = torch.stack((x, mixed), dim=-1)
        emb = self.embedding[None, None].expand(x.shape[0], x.shape[1], -1, -1)
        features = torch.cat((signal, emb), dim=-1).permute(0, 2, 1, 3)
        batch, nodes, steps, channels = features.shape
        _, h = self.rnn(features.reshape(batch * nodes, steps, channels))
        logits = self.output(h[-1]).reshape(batch, nodes)
        return self.horizon * logits.softmax(-1)


def model_for(name, graph, protocol):
    nodes, hidden = len(graph), protocol['hidden_units']
    horizon = protocol['calls_per_bin'] * protocol['target_bins']
    if name == 'temporal_gru':
        return TemporalGRU(nodes, hidden, horizon, protocol['calls_per_bin'])
    if name == 'stgnn_identity':
        graph = np.eye(nodes, dtype=np.float32)
    elif name != 'stgnn':
        raise ValueError(name)
    return STGNN(graph, hidden, horizon, protocol['calls_per_bin'])


def predict(model, x, batch_size=512):
    model.eval()
    with torch.inference_mode():
        return np.concatenate([model(torch.as_tensor(x[i:i+batch_size]).float()).numpy()
                               for i in range(0, len(x), batch_size)])
