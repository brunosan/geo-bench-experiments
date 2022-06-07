# Smallholder Cashew Dataset will be downloaded by torchgeo
#
# 1) This requires Radiant MLHub package and API token
#   pip install radiant_mlhub
# 2) Sign up for a MLHub account here: https://mlhub.earth/
# 3) Type this in your terminal:
#   mlhub configure
# and enter your API key.
#
# More info on the dataset: https://mlhub.earth/10.34911/rdnt.hfv20i

from ccb import io
from pathlib import Path
from tqdm import tqdm
from torchgeo.datasets import BeninSmallHolderCashews
import datetime
import os
from multiprocessing import Pool
from ccb.io.dataset import Sentinel2, CloudProbability

# Classification labels
LABELS = (
    "no data",
    "well-managed plantation",
    "poorly-managed plantation",
    "non-plantation",
    "residential",
    "background",
    "uncertain",
)
DATES = (
    "2019-11-05",
    "2019-11-10",
    "2019-11-15",
    "2019-11-20",
    "2019-11-30",
    "2019-12-05",
    "2019-12-10",
    "2019-12-15",
    "2019-12-20",
    "2019-12-25",
    "2019-12-30",
    "2020-01-04",
    "2020-01-09",
    "2020-01-14",
    "2020-01-19",
    "2020-01-24",
    "2020-01-29",
    "2020-02-08",
    "2020-02-13",
    "2020-02-18",
    "2020-02-23",
    "2020-02-28",
    "2020-03-04",
    "2020-03-09",
    "2020-03-14",
    "2020-03-19",
    "2020-03-24",
    "2020-03-29",
    "2020-04-03",
    "2020-04-08",
    "2020-04-13",
    "2020-04-18",
    "2020-04-23",
    "2020-04-28",
    "2020-05-03",
    "2020-05-08",
    "2020-05-13",
    "2020-05-18",
    "2020-05-23",
    "2020-05-28",
    "2020-06-02",
    "2020-06-07",
    "2020-06-12",
    "2020-06-17",
    "2020-06-22",
    "2020-06-27",
    "2020-07-02",
    "2020-07-07",
    "2020-07-12",
    "2020-07-17",
    "2020-07-22",
    "2020-07-27",
    "2020-08-01",
    "2020-08-06",
    "2020-08-11",
    "2020-08-16",
    "2020-08-21",
    "2020-08-26",
    "2020-08-31",
    "2020-09-05",
    "2020-09-10",
    "2020-09-15",
    "2020-09-20",
    "2020-09-25",
    "2020-09-30",
    "2020-10-10",
    "2020-10-15",
    "2020-10-20",
    "2020-10-25",
    "2020-10-30",
)
DATES = [datetime.datetime.strptime(date, "%Y-%m-%d").date() for date in DATES]

noclouds_25 = [2,
 3,
 4,
 5,
 6,
 7,
 8,
 9,
 10,
 12,
 13,
 15,
 16,
 17,
 19,
 20,
 22,
 23,
 27,
 28,
 30,
 33,
 37,
 38,
 69]  # 25 dates with the least clouds

ALL_BANDS = (
    Sentinel2("01 - Coastal aerosol", ("1", "01"), 60, 0.443),
    Sentinel2("02 - Blue", ("2", "02", "blue"), 10, 0.49),
    Sentinel2("03 - Green", ("3", "03", "green"), 10, 0.56),
    Sentinel2("04 - Red", ("4", "04", "red"), 10, 0.665),
    Sentinel2("05 - Vegetation Red Edge", ("5", "05"), 20, 0.705),
    Sentinel2("06 - Vegetation Red Edge", ("6", "06"), 20, 0.74),
    Sentinel2("07 - Vegetation Red Edge", ("7", "07"), 20, 0.783),
    Sentinel2("08 - NIR", ("8", "08", "NIR"), 20, 0.842),
    Sentinel2("08A - Vegetation Red Edge", ("8A", "08A"), 20, 0.865),
    Sentinel2("09 - Water vapour", ("9", "09"), 60, 0.945),
    Sentinel2("11 - SWIR", ("11",), 20, 1.61),
    Sentinel2("12 - SWIR", ("12",), 20, 2.19),
    CloudProbability(alt_names=("cloud coverage", "cloud mask"), spatial_resolution=10)
)

SPATIAL_RESOLUTION = 0.5  # meters, to be confirmed
N_TIMESTEPS = 70
LABEL_BAND = io.SegmentationClasses("label", spatial_resolution=SPATIAL_RESOLUTION, n_classes=len(LABELS))
GROUP_BY_TIMESTEP = False
NOCLOUDS = True

# Paths
DATASET_NAME = "smallholder_cashew"
SRC_DATASET_DIR = Path(io.src_datasets_dir, DATASET_NAME)
DATASET_DIR = Path(io.datasets_dir, DATASET_NAME)


def get_sample_name(total_samples):
    return f"sample_{total_samples}"


def convert(max_count=None, dataset_dir=DATASET_DIR):
    dataset_dir.mkdir(exist_ok=True, parents=True)

    print("Loading dataset from torchgeo")
    cashew = BeninSmallHolderCashews(root=SRC_DATASET_DIR, download=True, checksum=True)

    task_specs = io.TaskSpecifications(
        dataset_name=DATASET_NAME,
        patch_size=(256, 256),
        n_time_steps=len(noclouds_25) if NOCLOUDS else N_TIMESTEPS,
        bands_info=ALL_BANDS,
        bands_stats=None,  # Will be automatically written with the inspect script
        label_type=LABEL_BAND,
        eval_loss=io.SegmentationAccuracy,  # TODO probably not the final loss eval loss. To be discussed.
        # either 50cm or 40cm, Airbus Pleiades 50cm, https://radiantearth.blob.core.windows.net/mlhub/technoserve-cashew-benin/Documentation.pdf
        spatial_resolution=SPATIAL_RESOLUTION,
    )

    partition = io.Partition()

    task_specs.save(dataset_dir, overwrite=True)

    print("Saving timesteps as separate bands")
    total_samples = 0
    for tg_sample in tqdm(cashew):

        images = tg_sample["image"].numpy()
        mask = tg_sample["mask"].numpy()
        n_timesteps, n_bands, _height, _width = images.shape

        label = io.Band(
            data=mask, band_info=LABEL_BAND, spatial_resolution=SPATIAL_RESOLUTION, transform=None, crs=None
        )
        split = np.random.choice(("train", "valid", "test"), p=(0.8, 0.1, 0.1))
        grouped_bands = []
        for date_idx in range(n_timesteps):
            current_bands = []
            if NOCLOUDS and date_idx not in noclouds_25:
                continue

            for band_idx in range(n_bands):
                band_data = images[date_idx, band_idx, :, :]

                band_info = ALL_BANDS[band_idx]

                band = io.Band(
                    data=band_data,
                    band_info=band_info,
                    date=DATES[date_idx],
                    spatial_resolution=SPATIAL_RESOLUTION,
                    transform=None,  # TODO can't find the GPS coordinates from torch geo.
                    crs=None,
                    convert_to_int16=False,
                )
                current_bands.append(band)
                grouped_bands.append(band)

            if not GROUP_BY_TIMESTEP:
                sample = io.Sample(current_bands, label=label, sample_name=get_sample_name(total_samples))
                sample.write(dataset_dir)
                partition.add(split, get_sample_name(total_samples))
                total_samples += 1

            if max_count is not None and total_samples >= max_count:
                break

        if GROUP_BY_TIMESTEP:
            sample = io.Sample(grouped_bands, label=label, sample_name=get_sample_name(total_samples))
            sample.write(dataset_dir)
            partition.add(split, get_sample_name(total_samples))
            total_samples += 1

        if max_count is not None and total_samples >= max_count:
            break

    # partition.resplit_iid(split_names=("train", "valid", "test"), ratios=(0.8, 0.1, 0.1))
    partition.save(dataset_dir, "default")
    print(f"Done. GROUP_BY_TIMESTEP={GROUP_BY_TIMESTEP}, total_samples={total_samples}")


if __name__ == "__main__":
    convert()
