from typing import List
from ccb import io
from ccb.experiment.experiment import hparams_to_string
from ccb.io.task import TaskSpecifications
from ccb.torch_toolbox.model import BackBone, ModelGenerator, Model, head_generator, train_loss_generator, collate_rgb
import torch.nn.functional as F
import torch


class Conv4Generator(ModelGenerator):
    def generate(self, task_specs: TaskSpecifications, hyperparameters: dict):
        """Returns a ccb.torch_toolbox.model.Model instance from task specs
           and hyperparameters

        Args:
            task_specs (TaskSpecifications): object with task specs
            hyperparameters (dict): dictionary containing hyperparameters
        """
        backbone = Conv4(self.model_path, task_specs, hyperparameters)
        head = head_generator(task_specs, hyperparameters)
        loss = train_loss_generator(task_specs, hyperparameters)
        return Model(backbone, head, loss, hyperparameters)

    def hp_search(self, task_specs, max_num_configs=10):
        hparams1 = {
            "lr_milestones": (10, 20),
            "lr_gamma": 0.1,
            "lr_backbone": 1e-3,
            "lr_head": 2e-3,
            "head_type": "linear",
            "train_iters": 50,
            "features_shape": (64,),
            "loss_type": "crossentropy",
            "batch_size": 32,
            "num_workers": 4,
            "logger": "csv",
        }

        hparams2 = hparams1.copy()
        hparams2["lr_head"] = 4e-3

        return hparams_to_string([hparams1, hparams2])

    def get_collate_fn(self, task_specs: TaskSpecifications, hparams: dict):

        if task_specs.dataset_name.lower() == "mnist":
            return None  # will use torch's default collate function.
        else:
            return collate_rgb


model_generator = Conv4Generator()


class Conv4(BackBone):
    def __init__(self, model_path, task_specs: io.TaskSpecifications, hyperparams):
        super().__init__(model_path, task_specs, hyperparams)
        n_bands = min(3, len(task_specs.bands_info))
        self.conv0 = torch.nn.Conv2d(n_bands, 64, 3, 1, 1)
        self.conv1 = torch.nn.Conv2d(64, 64, 3, 1, 1)
        self.conv2 = torch.nn.Conv2d(64, 64, 3, 1, 1)
        self.conv3 = torch.nn.Conv2d(64, 64, 3, 1, 1)

    def forward(self, x):
        x = F.relu(self.conv0(x), True)
        x = F.relu(self.conv1(x), True)
        x = F.max_pool2d(x, 3, 2, 1)
        x = F.relu(self.conv2(x), True)
        x = F.max_pool2d(x, 3, 2, 1)
        x = F.relu(self.conv3(x), True)
        x = F.max_pool2d(x, 3, 2, 1)
        return x.mean((2, 3))