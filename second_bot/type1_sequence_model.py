"""Training-only neural architecture for five-digit type1 CAPTCHA sequences.

The model consumes the full image and predicts a digit sequence with CTC; it
never assumes that each glyph occupies a fixed-width crop. PyTorch is needed
only for training/export, not by the Discord bot's current runtime.
"""
from __future__ import annotations

import torch
from torch import nn
import torch.nn.functional as F

IMAGE_WIDTH = 320
IMAGE_HEIGHT = 112
CELL_COUNT = 5
CLASS_COUNT = 11  # CTC blank (0), then digits 0..9 (1..10)
BLANK_INDEX = 0


class ConvStage(nn.Sequential):
    def __init__(self, in_channels: int, out_channels: int, pool: tuple[int, int]) -> None:
        super().__init__(
            nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(kernel_size=pool, stride=pool),
        )


class Type1SequenceOCR(nn.Module):
    """Compact CRNN with a CTC sequence head and an auxiliary position head."""

    def __init__(self) -> None:
        super().__init__()
        self.backbone = nn.Sequential(
            ConvStage(1, 32, (2, 2)),
            ConvStage(32, 64, (2, 2)),
            ConvStage(64, 96, (2, 1)),
            ConvStage(96, 128, (2, 1)),
        )
        self.sequence = nn.LSTM(
            input_size=128,
            hidden_size=96,
            num_layers=1,
            bidirectional=True,
            batch_first=True,
        )
        self.dropout = nn.Dropout(0.15)
        self.ctc_head = nn.Linear(192, CLASS_COUNT)
        self.position_head = nn.Linear(192, 10)

    def forward(self, images: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        features = self.backbone(images)
        sequence = features.mean(dim=2).transpose(1, 2)  # [B, T, C]
        sequence, _ = self.sequence(sequence)
        sequence = self.dropout(sequence)
        sequence_logits = self.ctc_head(sequence)  # [B, T, blank+digits]

        position_features = F.adaptive_avg_pool1d(
            sequence.transpose(1, 2), CELL_COUNT
        ).transpose(1, 2)
        position_logits = self.position_head(self.dropout(position_features))
        return sequence_logits, position_logits
