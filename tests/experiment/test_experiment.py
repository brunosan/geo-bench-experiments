import os
import subprocess
import sys
import tempfile
from pathlib import Path

import geobench_exp
import pytest
from geobench_exp.experiment.experiment import Job, get_model_generator
from geobench_exp.experiment.sequential_dispatcher import sequential_dispatcher
from ruamel.yaml import YAML


def test_load_model():
    """Test loading an existing model generator from a user-specified path."""
    model_generator = get_model_generator("geobench_exp.torch_toolbox.model_generators.conv4")
    assert hasattr(model_generator, "generate_model")


def test_load_trainer():
    """Test loading an existing model generator from a user-specified path."""
    model_generator = get_model_generator("geobench_exp.torch_toolbox.model_generators.conv4")
    assert hasattr(model_generator, "generate_trainer")


def test_unexisting_path():
    """
    Test trying to load from an unexisting module path.

    """
    try:
        get_model_generator("geobench_exp.torch_toolbox.model_generators.foobar")
    except Exception as e:
        assert isinstance(e, ModuleNotFoundError)


@pytest.mark.slow
@pytest.mark.parametrize(
    "config_filepath",
    [
        ("tests/configs/base_classification.yaml"),
        ("tests/configs/base_segmentation.yaml"),
    ],
)
def test_experiment_generator_on_benchmark(config_filepath):

    experiment_generator_dir = Path(geobench_exp.experiment.__file__).absolute().parent

    with tempfile.TemporaryDirectory(prefix="test") as generate_experiment_dir:
        # change experiment dir to tmp path
        yaml = YAML()
        with open(config_filepath, "r") as yamlfile:
            config = yaml.load(yamlfile)

        config["experiment"]["generate_experiment_dir"] = generate_experiment_dir

        new_config_filepath = os.path.join(generate_experiment_dir, "config.yaml")
        with open(new_config_filepath, "w") as fd:
            yaml.dump(config, fd)

        print(f"Generating experiments in {generate_experiment_dir}.")
        cmd = [
            sys.executable,
            str(experiment_generator_dir / "experiment_generator.py"),
            "--config_filepath",
            new_config_filepath,
        ]

        subprocess.check_call(cmd)
        os.remove(new_config_filepath)
        exp_dir = os.path.join(generate_experiment_dir, os.listdir(generate_experiment_dir)[0])
        sequential_dispatcher(exp_dir=exp_dir, prompt=False)
        for ds_dir in Path(exp_dir).iterdir():
            job = Job(ds_dir)
            print(ds_dir)
            metrics = job.get_metrics()
            print(metrics)


if __name__ == "__main__":
    test_experiment_generator_on_benchmark()
