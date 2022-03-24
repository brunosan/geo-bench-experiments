# Convenient code for bypassing commandlines
# This will create experiments and lanch on toolkit
# This file serves as a template and will be remove from the repo once we're done with experiments.
# Please don't commit the changes related to your personnal experiments

from pathlib import Path
from ccb.experiment.experiment_generator import experiment_generator
from toolkit import dispatch_toolkit


expirment_dir = experiment_generator(
    model_generator_module_name="ccb.torch_toolbox.model_generators.conv4",
    experiment_dir="/mnt/data/experiments/allac",  # make sure this datamodule is mounted like this: snow.rg_climate_benchmark.data:/mnt/data
    benchmark_name="classification",
    experiment_name="test_conv4",
)


dispatch_toolkit.push_code(Path(__file__).parent.parent)
dispatch_toolkit.toolkit_dispatcher(expirment_dir)