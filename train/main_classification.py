from pathlib import Path
from copy import deepcopy
from argparse import ArgumentParser
import os
import sys
import pickle
import torch
from torch import nn, optim
from torchvision.models import resnet
import pytorch_lightning as pl
from pytorch_lightning import LightningModule, Trainer
from pytorch_lightning.metrics import Accuracy
from pytorch_lightning.loggers import TensorBoardLogger
import numpy as np

# import clip

from datasets.eurosat_datamodule import EurosatDataModule
from datasets.sat_datamodule import SatDataModule
from models.moco2_module import MocoV2
from utils.utils import PretrainedModelDict, hp_to_str

# from models.clip_module import CLIPEncoder
# import onnx
# from onnx2pytorch import ConvertModel


class Permute(torch.nn.Module):
    def __init__(self, dims):
        super(Permute, self).__init__()
        self.dims = dims

    def forward(self, x):
        return x.permute(self.dims)


class Classifier(LightningModule):
    def __init__(self, in_features, num_classes, backbone=None):
        super().__init__()
        self.encoder = backbone
        self.classifier = nn.Linear(in_features, num_classes)
        self.criterion = nn.CrossEntropyLoss()
        self.accuracy = Accuracy()

    def forward(self, x):
        if self.encoder:
            x = self.encoder(x)
        x = x.float()
        logits = self.classifier(x)
        return logits

    def training_step(self, batch, batch_idx):

        loss, acc = self.shared_step(batch)
        self.log("train/loss", loss, prog_bar=True)
        self.log("train/acc", acc, prog_bar=True)
        return loss

    def validation_step(self, batch, batch_idx):
        loss, acc = self.shared_step(batch)
        self.log("val/loss", loss, prog_bar=True)
        self.log("val/acc", acc, prog_bar=True)

        return loss

    def shared_step(self, batch):
        x, y = batch
        logits = self(x)
        loss = self.criterion(logits, y)
        acc = self.accuracy(torch.argmax(logits, dim=1), y)
        return loss, acc

    def configure_optimizers(self):

        max_epochs = self.trainer.max_epochs
        optimizer_params = [{"params": self.classifier.parameters(), "lr": args.lr}]

        if self.encoder:
            optimizer_params.append({"params": self.encoder.parameters(), "lr": args.backbone_lr})

        optimizer = optim.Adam(optimizer_params, weight_decay=args.weight_decay)
        scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=[int(0.6 * max_epochs), int(0.8 * max_epochs)])

        return [optimizer], [scheduler]


if __name__ == "__main__":
    pl.seed_everything(42)

    parser = ArgumentParser()
    parser.add_argument("--gpus", type=int, default=1)
    parser.add_argument("--data_dir", type=str)
    parser.add_argument("--module", type=str)
    parser.add_argument("--num_workers", type=int, default=8)

    parser.add_argument("--backbone_type", type=str, default="imagenet")
    parser.add_argument("--dataset", type=str, default="eurosat")
    parser.add_argument("--ckpt_path", type=str, default=None)
    parser.add_argument("--finetune", action="store_true")

    parser.add_argument("--batch_size", type=int, default=128)
    parser.add_argument("--lr", type=float, default=0.001)
    parser.add_argument("--backbone_lr", type=float, default=0.001)
    parser.add_argument("--max_epochs", type=int, default=100)
    parser.add_argument("--weight_decay", type=float, default=0)

    args = parser.parse_args()

    pmd = PretrainedModelDict()

    if args.backbone_type == "random":
        backbone = resnet.resnet18(pretrained=False)
        backbone = nn.Sequential(*list(backbone.children())[:-1], nn.Flatten())
    elif args.backbone_type == "imagenet":
        backbone = resnet.resnet18(pretrained=True)
        backbone = nn.Sequential(*list(backbone.children())[:-1], nn.Flatten())
    elif args.backbone_type == "pretrain":  # to load seco
        model = MocoV2.load_from_checkpoint(args.ckpt_path)
        backbone = deepcopy(model.encoder_q)
    elif args.backbone_type == "custom":
        backbone = torch.load(args.ckpt_path)

    # elif args.backbone_type in pmd.get_available_models(): # only tested resnet18 for now
    #     backbone = pmd.get_model(args.backbone_type)

    #     # print(list(backbone.children()))
    #     backbone = nn.Sequential(*list(backbone.children())[:-1], nn.Flatten())

    # elif args.backbone_type in clip.available_models(): # currently get nan losses
    #     # ['RN50', 'RN101', 'RN50x4', 'RN50x16', 'ViT-B/32', 'ViT-B/16']
    #     model, preprocess = clip.load(args.backbone_type)
    #     backbone = CLIPEncoder(model, preprocess)

    # elif args.backbone_type == 'onnx':
    #     model = onnx.load(args.ckpt_path)
    #     backbone = ConvertModel(model)#, experimental=True)# , debug=True
    #     backbone = nn.Sequential(Permute((0, 2, 3, 1)),backbone, nn.Flatten())

    else:
        raise ValueError('backbone_type must be one of "random", "imagenet", "custom" or "pretrain"')

    if args.dataset == "eurosat":
        datamodule = EurosatDataModule(args)
    elif args.dataset == "sat":
        datamodule = SatDataModule(args)
    else:
        raise ValueError('dataset must be one of "sat" or "eurosat"')

    if args.finetune:
        model = Classifier(in_features=512, num_classes=datamodule.num_classes, backbone=backbone)
        # model.example_input_array = torch.zeros((1, 3, 64, 64))

    else:
        datamodule.add_encoder(backbone)
        model = Classifier(in_features=512, num_classes=datamodule.num_classes)

    experiment_name = hp_to_str(args)

    os.makedirs(os.path.join(Path.cwd(), "logs", experiment_name), exist_ok=True)
    if args.no_logs:
        logger = TensorBoardLogger(save_dir=str(Path.cwd() / "logs"), name=experiment_name)
    else:
        logger = False

    trainer = Trainer(
        gpus=args.gpus, logger=logger, checkpoint_callback=False, max_epochs=args.max_epochs, weights_summary="full"
    )

    trainer.fit(model, datamodule=datamodule)
    print(trainer.callback_metrics)

    with open(str(Path.cwd() / "logs" / experiment_name / "max_val"), "w") as f:
        f.write("max_accuracy: {}".format(torch.max(trainer.callback_metrics["val/acc"].item())))