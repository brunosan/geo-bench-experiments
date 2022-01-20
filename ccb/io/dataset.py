import ast
import pathlib
import numpy as np
from numpy.lib.function_base import percentile
import rasterio
import json
from pathlib import Path
import datetime
from typing import List, Union, Set, Dict, Tuple, Optional
import os
from scipy.ndimage import zoom
import pickle
from functools import cached_property, lru_cache
from warnings import warn
from ccb.io.task import LabelType, TaskSpecifications
from collections import OrderedDict


src_datasets_dir = os.environ.get("CC_BENCHMARK_SOURCE_DATASETS", os.path.expanduser("~/dataset/"))
datasets_dir = os.environ.get("CC_BENCHMARK_CONVERTED_DATASETS", os.path.expanduser("~/converted_dataset/"))


class BandInfo(object):
    """Base class for storing non pixel information about bands such as band name, wavelenth and spatial resolution."""

    def __init__(self, name=None, alt_names=(), spatial_resolution=None) -> None:
        """
        Args:
            name: The main name of the band. This name is used for sorting the band and providing an order.
            alt_names: a tuple of alternative names for referencing to the band. e.g., red, green, blue, NIR.
            spatial_resolution: original spatial resolution of this band.
        """
        self.name = name
        self.alt_names = alt_names
        self.spatial_resolution = spatial_resolution

    def __key(self):
        return self.name

    def __hash__(self):
        return hash(self.__key())

    def __eq__(self, other):
        if type(other) is type(self):
            return self.__key() == other.__key()
        else:
            return False

    def __lt__(self, other):
        return self.__key() < other.__key()

    def assert_valid(self, band):
        assert isinstance(band, Band)
        assert band.band_info == self, f"{str(band.band_info)} vs {str(self)}"
        assert isinstance(band.data, np.ndarray)
        if not (band.data.dtype == np.int16):
            warn(f"band.data is expected to be int16, but has type {band.data.dtype}")
        if band.transform is None:
            warn(f"No geotransformation specified for band {band.band_info.name}.")

    def __str__(self):
        return f"Band {self.name} ({self.spatial_resolution:.1f}m resolution)"

    def expand_name(self):
        return [self.name]


class SpectralBand(BandInfo):
    """Extends BandInfo to provide wavelength of the band."""

    def __init__(self, name=None, alt_names=(), spatial_resolution=None, wavelength=None) -> None:
        super().__init__(name, alt_names, spatial_resolution)
        self.wavelength = wavelength

    def __key(self):
        return (self.name, self.wavelength)


class Sentinel2(SpectralBand):
    "Spectral band of type Sentinel2"


class Mask(BandInfo):
    pass


class Height(BandInfo):
    pass


class MultiBand(BandInfo):
    """Contains a 3d object with multiple band of the same resolution e.g. HyperSpectralBands"""

    def __init__(self, name=None, alt_names=(), spatial_resolution=None, n_bands=None) -> None:
        super().__init__(name=name, alt_names=alt_names, spatial_resolution=spatial_resolution)
        self.n_bands = n_bands

    def expand_name(self):
        return [self.name] * self.n_bands


class HyperSpectralBands(MultiBand):
    def __init__(self, name=None, alt_names=(), spatial_resolution=None, n_bands=None, wavelength_range=None) -> None:
        super().__init__(name=name, alt_names=alt_names, spatial_resolution=spatial_resolution, n_bands=n_bands)
        self.wavelength_range = wavelength_range


class CloudProbability(Mask):
    def __init__(self, alt_names=(), spatial_resolution=None) -> None:
        super().__init__("Cloud Probability", alt_names=alt_names, spatial_resolution=spatial_resolution)


class SegmentationClasses(BandInfo, LabelType):
    def __init__(self, name, spatial_resolution, n_classes, class_names=None) -> None:
        super().__init__(name=name, spatial_resolution=spatial_resolution)
        self.n_classes = n_classes
        if class_names is not None:
            assert len(class_names) == n_classes, f"{len(class_names)} vs {n_classes}"
        self.class_name = class_names

    def assert_valid(self, value):
        assert isinstance(value, Band)
        assert value.band_info == self
        assert np.all(value.data >= 0)
        assert np.all(value.data < self.n_classes)


sentinel2_13_bands = [
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
    Sentinel2("10 - SWIR - Cirrus", ("10",), 60, 1.375),
    Sentinel2("11 - SWIR", ("11",), 20, 1.61),
    Sentinel2("12 - SWIR", ("12",), 20, 2.19),
]


class Band:
    """Group Band information and provide function for saving to geotiff"""

    def __init__(
        self,
        data: np.ndarray,
        band_info: BandInfo,
        spatial_resolution: float,
        date: Union[datetime.datetime, datetime.date] = None,
        date_id=None,
        transform=None,
        crs=None,
        meta_info=None,
        convert_to_int16: bool = True,
    ) -> None:
        """
        Args:
            data: 2d or 3d array of data containing the pixels of the band. shape=(height, width) or shape=(height, width, bands) 
            band_info: Object of type Band_Info containing the band name, wavelength, spatial_resolution original spatial resolution.
            spatial_resolution: current Spatial resolution of the pixels in meters. Note: Band.band_info.spatial_resolution  contains
                the original spatial resolution of the sensor. If data is a resampled version of the original data, Band.spatial_resolution
                must contain the new spatial resolution.
            date: The data this data was acquired
            date_id: used for odering and group the dates when dataset contains time series.
            transform: georeferncing transformation as provided by rasterio. See rasterio.transform.from_bounds for example.
            crs: coordinate refenence system for the transformation.
            meta_info: A dict of any information that might be useful.
            convert_to_int16: By default, data will be converted to int16 when saved to_geotiff. ValueError will be raised if conversion is 
                not possible. Set this flag to False to bypass this mechanism.
        """
        self.data = data
        self.band_info = band_info
        self.spatial_resolution = spatial_resolution
        self.date = date
        self.date_id = date if date_id is None else date_id
        self.transform = transform
        self.crs = crs
        self.meta_info = meta_info
        self.convert_to_int16 = convert_to_int16

    def get_descriptor(self):
        descriptor = self.band_info.name
        if self.date is not None:
            descriptor += f"_{_format_date(self.date)}"
        return descriptor

    def write_to_geotiff(self, directory, band_idx):
        """
        Write an image from an array to a geotiff file with its label.

        We compress with zstd, a lossless compression which gains a factor of ~2 in compression.
        Write speed can be 4x-5x slower and read speed ~2x slower.
        Interesting benchmark can be found here
        https://kokoalberti.com/articles/geotiff-compression-optimization-guide/

        Arguments:
            directory: Destination path to save the file.

        Raises:
            ValueError: when values of image are not in range (-32768, 32767)
        """
        data = self.data
        assert type(data) == np.ndarray, f"got type {type(data)}."

        if data.ndim == 2:
            data = np.expand_dims(data, 2)

        # if data.ndim == 3 and data.shape[2] == 1:
        #     if not isinstance(self.band_info, MultiBand):
        #         data = np.squeeze(data, axis=2)
        #         warn("data has a 3rd dimension of size 1. Squeezing it out and continuing.")

        if self.convert_to_int16:

            if np.min(data) < -32768 or np.max(data) > 32767:
                raise ValueError("Data out of range. Will not convert to int16.")

            if np.sum(np.logical_and(data > 1e-6, data <= 0.5)) > 0:
                raise ValueError(
                    "Float value between 1e-6 and 0.5 would be converted to 0 when casting to int16, which is the nodata value."
                )

            data = np.round(data).astype(np.int16)

        file_path = Path(directory, f"{band_idx:02}_{self.get_descriptor()}.tif")
        with rasterio.open(
            file_path,
            "w",
            driver="GTiff",
            height=data.shape[0],
            width=data.shape[1],
            count=data.shape[2],
            dtype=data.dtype,
            crs=self.crs,
            compress="zstd",
            predictor=2,
            transform=self.transform,
        ) as dst:

            tags = dict(
                date=self.date,
                date_id=self.date_id,
                spatial_resolution=self.spatial_resolution,
                band_info=self.band_info,
                meta_info=self.meta_info,
            )
            dst.update_tags(data=str(pickle.dumps(tags)))

            dst.nodata = 0  # we use 0 as the nodata value.

            dst.write(np.moveaxis(data, 2, 0))

            if data.shape[2] == 1:
                dst.set_band_description(1, self.band_info.name)
            else:
                for i in range(data.shape[2]):
                    dst.set_band_description(i + 1, f"{i:03d}_{self.band_info.name}")

        return file_path


def load_band(file_path):
    with rasterio.open(file_path) as src:
        tags = pickle.loads(ast.literal_eval(src.tags()["data"]))
        data = src.read()
        if data.ndim == 3:
            data = np.moveaxis(data, 0, 2)

        band_info = tags["band_info"]
        if not isinstance(band_info, MultiBand) and data.ndim == 3:
            assert data.shape[2] == 1, f"Got shape: {data.shape}."
            data = np.squeeze(data, axis=2)
        band = Band(data=data, transform=src.transform, crs=src.crs, **tags)

    return band


def _make_map(elements):
    elements = list(elements)
    elements.sort()
    element_map = {element: i for i, element in enumerate(elements)}
    return element_map, elements


def _map_bands(band_info_set):
    band_info_list = list(band_info_set)
    # band_info_list.sort()

    band_name_map = {}
    for band_idx, band_info in enumerate(band_info_list):
        band_name_map[band_info.name] = band_idx
        for alt_name in band_info.alt_names:
            band_name_map[alt_name] = band_idx

    return band_name_map, band_info_list


# TODO need to make sure that band order is consistant through the dataset
class Sample(object):
    def __init__(self, bands: List[Band], label: Union[LabelType, float, int], sample_name: str) -> None:
        super().__init__()
        self.bands = bands
        self.label = label
        self.sample_name = sample_name
        self._build_index()

    def _build_index(self):

        dates = set()
        band_info_set = OrderedDict()  # using it as an ordered set
        bands = self.bands

        for band in bands:
            dates.add(band.date)
            band_info_set[band.band_info] = None

        self.date_map, self.dates = _make_map(dates)
        self.band_name_map, self.band_info_list = _map_bands(band_info_set.keys())
        self.band_names = [band_info.name for band_info in self.band_info_list]

        self.band_array = np.empty((len(self.dates), len(self.band_info_list)), dtype=np.object)

        for band in bands:
            band_idx = self.band_name_map[band.band_info.name]
            date_idx = self.date_map[band.date]
            self.band_array[date_idx, band_idx] = band

    def get_band_info(self, band_name):
        return self.band_info_list[self.band_name_map[band_name]]

    def is_time_series(self):
        return len(self.dates) > 1

    def pack_to_4d(
        self,
        dates=None,
        band_names: Tuple[str] = None,
        resample: bool = False,
        fill_value: float = None,
        resample_order: int = 3,
    ) -> Tuple[np.ndarray, List[datetime.date], List[str]]:
        """Pack all bands into an 4d array of shape (n_dates, height, width, n_bands). If it contains MultiBands, the final
        dimension 

        Args:
            dates: Selects a subset of dates. Defaults to None, which selects all dates.
            band_names: Selects a subset of bands with a list of string. Defaults to None, which selects all bands.
                Will search into band_info.name and band_info.alt_names. You cen use, e.g., ('red', 'green', 'blue').
            resample: will enable resampling bands to match the largest shape. Defaults to False and raises an error
                if bands are not all the same shape. Resampling is performed using scipy.ndimage.zoom with order `resample_order`
            fill_value: Fills missing bands with this value. Defaults to None, which will raise an error for missing bands. 
                The type or np.dtype of fill_value may influence the numerical precision of the returned array. See numpy.array's documentation.
            resample_order: passed to scipy.ndimage.zoom when resampling.

        Returns:
            array: 4d array of (n_dates, height, width, n_bands) containing the packed data.
            dates: selected dates
            band_names: selected bands
        """
        band_array, dates, band_names = self.get_band_array(dates, band_names)
        shape = _largest_shape(band_array)
        data_grid = []
        for i in range(band_array.shape[0]):
            data_list = []
            for j in range(band_array.shape[1]):
                band = band_array[i, j]

                if band is None:
                    if fill_value is not None:
                        # TODO doesn't work yet with MultiBand will raise an error when concatenating
                        data_list.append(np.zeros(shape, dtype=np.int16) + fill_value)
                    else:
                        raise ValueError(f"Missing band {band_names[j]} for date {dates[i]:s}, but fill_vlaue is None.")
                else:
                    data = band.data
                    if data.ndim == 2:
                        data = np.expand_dims(data, 2)
                    if data.shape[:2] != shape:
                        if resample:
                            zoom_factor = np.concatenate((np.array(shape) / np.array(data.shape[:2]), [1]))
                            assert zoom_factor[0] == zoom_factor[1]
                            data_list.append(zoom(data, zoom=zoom_factor, order=resample_order))
                        else:
                            raise ValueError(
                                f"Band {band_names[j]} has shape {data.shape:s}, max shape is {shape:s}, but resample is set to False."
                            )
                    else:
                        data_list.append(data)
            data_grid.append(np.concatenate(data_list, axis=2))

        nand_names_ = []
        for name in band_names:
            nand_names_.extend(self.get_band_info(name).expand_name())

        array = np.array(data_grid)
        return array, dates, nand_names_

    def get_band_array(self, dates=None, band_names=None):
        band_array = self.band_array

        if band_names is not None:
            band_indexes = [self.band_name_map[band_name] for band_name in band_names]
            band_array = band_array[:, band_indexes]
        else:
            band_names = self.band_names

        if dates is not None:
            date_indexes = [self.date_map[date] for date in dates]
            band_array = band_array[date_indexes, :]
        else:
            dates = self.dates

        return band_array, dates, band_names

    def pack_to_3d(self, band_names=None, resample=False, fill_value=None, resample_order=3):
        data_4d, _, band_names = self.pack_to_4d(
            band_names=band_names, resample=resample, fill_value=fill_value, resample_order=resample_order
        )
        assert data_4d.shape[0] == 1
        return data_4d[0], band_names

    def write(self, dataset_dir):

        dst_dir = pathlib.Path(dataset_dir, self.sample_name)
        dst_dir.mkdir(exist_ok=True, parents=True)

        file_set = set()
        for i, band in enumerate(self.bands):
            file_set.add(band.write_to_geotiff(dst_dir, i))
        if len(file_set) != len(self.bands):
            raise ValueError(f"Duplicate band description in bands.")

        if self.label is not None:
            if isinstance(self.label, Band):
                if not isinstance(self.label.band_info, LabelType):
                    raise ValueError("The label is of type Band, but its band_info is not instance of Label.")
                self.label.write_to_geotiff(dst_dir, -1)
            else:
                with open(Path(dst_dir, "label.json"), "w") as fd:
                    json.dump(self.label, fd)


def load_sample(sample_dir):
    sample_dir = Path(sample_dir)
    band_list = []
    label = None
    for file in sample_dir.iterdir():
        if file.name == "label.json":
            with open(file, "r") as fd:
                label = json.load(fd)
        elif file.name.endswith(".aux.xml"):
            continue
        else:
            band_idx = int(file.name.split('_')[0])
            band_list.append((band_idx, load_band(file)))

    band_list.sort(key=lambda x: x[0])  # sort according to band_idx
    _, band_list = zip(*band_list)  # discard band_idx
    band_list = list(band_list)

    if label is None:
        label = _extract_label(band_list)
    return Sample(band_list, label, sample_name=sample_dir.name)


def _largest_shape(band_array):
    """Extract the largest shape and the dtype from an array of bands. 
    Assertion error is raised if there is more than one type."""
    shape = [0, 0]
    for band in band_array.flat:
        if band is None:
            continue
        shape[0] = max(shape[0], band.data.shape[0])
        shape[1] = max(shape[1], band.data.shape[1])

    return tuple(shape)


def _extract_label(band_list):
    """Extract the label information from the band_list. *Note, the band_list is modified.*"""
    labels = set()
    for idx in range(len(band_list) - 1, -1, -1):  # iterate backward to avoid changing list index when popping
        if isinstance(band_list[idx].band_info, LabelType):
            labels.add(band_list.pop(idx))

    labels.discard(None)
    if len(labels) != 1:
        raise ValueError(f"Found {len(labels)} label while expecting exactly 1 label.")
    return labels.pop()


class Partition(dict):
    def __init__(self, partition_dict=None, map=None) -> None:
        self.map = map
        if partition_dict is None:
            self.partition_dict = {"train": [], "valid": [], "test": []}
        else:
            self.partition_dict = partition_dict

    def add(self, key, value):
        if key in self.map:
            key = self.map[key]
        self.partition_dict[key].append(value)

    def save(self, directory, partition_name):
        file_path = Path(directory, partition_name + ".json")
        with open(file_path, "w") as fd:
            json.dump(self.partition_dict, fd, indent=2)


class GeneratorWithLength(object):
    """A generator containing its length. Useful for e.g., tqdm."""

    def __init__(self, generator, length):
        self.generator = generator
        self.length = length

    def __len__(self):
        return self.length

    def __iter__(self):
        return self.generator


class Dataset:
    def __init__(self, dataset_dir, active_partition="default") -> None:
        """
        Args:
            dataset_dir: the path containing the samples of the dataset.
            active_parition: Each dataset can have more than 1 partiiton. Use this field to specify the active_partition.
        """
        self.dataset_dir = Path(dataset_dir)
        self._task_specs_path = None
        self.active_partition = active_partition
        self._load_path_list()
        self._load_partition()

    def _load_path_list(self) -> None:
        self._partition_path_dict = {}
        self._sample_path_list = []
        for p in self.dataset_dir.iterdir():
            if p.name.endswith("_partition.json"):
                partition_name = p.name.split("_partition.json")[0]
                self._partition_path_dict[partition_name] = p
            elif p.name == "task_specifications.pkl":
                self._task_specs_path = p
            else:
                self._sample_path_list.append(p)

    def _load_partition(self):
        if len(self._partition_path_dict) == 0:
            warn(f"No partition found for dataset {self.dataset_dir.name}.")
            return

        if "default" not in self._partition_path_dict:
            partition_name = None
            if "original" in self._partition_path_dict:
                partition_name = "original"
            else:
                partition_name = self._partition_path_dict.keys()[0]

            self._partition_path_dict["default"] = self._partition_path_dict[partition_name]
            warn(f"No default partition found for dataset {self.dataset_dir.name}. Using {partition_name} as default.")

        self.set_active_partition(partition_name="default")

    def _iter_dataset(self, max_count=None):
        path_list = np.random.choice(self._sample_path_list, size=max_count, replace=False)
        for directory in path_list:
            yield load_sample(directory)

    def iter_dataset(self, max_count=None):
        n = len(self._sample_path_list)
        if max_count is None:
            max_count = n
        else:
            max_count = min(n, max_count)

        return GeneratorWithLength(self._iter_dataset(max_count=max_count), max_count)

    @cached_property
    def task_specs(self) -> TaskSpecifications:
        if self._task_specs_path is None:
            raise ValueError(f"The file 'task_specifications.pkl' does not exist for dataset {self.dataset_dir.name}.")
        with open(self._task_specs_path, "rb") as fd:
            return pickle.load(fd)

    def list_partitions(self):
        return self._partition_path_dict.keys()

    def set_active_partition(self, partition_name="default"):
        if partition_name not in self._partition_path_dict:
            raise ValueError(f"Unknown partition {partition_name}.")
        self.active_partition_name = partition_name
        self.active_partition = self.get_partition(partition_name)

    @lru_cache(maxsize=3)
    def get_partition(self, partition_name="default"):
        with open(self._partition_path_dict[partition_name], "r") as fd:
            return json.load(fd)

    def __len__(self):
        return len(self._sample_path_list)


class Stats:
    def __init__(
        self, min, max, mean, std, median, percentile_0_1, percentile_1, percentile_99, percentile_99_9
    ) -> None:
        self.min = min
        self.max = max
        self.mean = mean
        self.std = std
        self.median = median
        self.percentile_0_1 = percentile_0_1
        self.percentile_1 = percentile_1
        self.percentile_99 = percentile_99
        self.percentile_99_9 = percentile_99_9


def compute_stats(values):
    q_0_1, q_1, median, q_99, q_99_9 = np.percentile(values, q=[0.1, 1, 50, 99, 99.9])
    stats = Stats(
        min=np.min(values),
        max=np.max(values),
        mean=np.mean(values),
        std=np.std(values),
        median=median,
        percentile_0_1=q_0_1,
        percentile_1=q_1,
        percentile_99=q_99,
        percentile_99_9=q_99_9,
    )
    return stats


def _format_date(date: Union[datetime.date, datetime.datetime]):
    if isinstance(date, datetime.date):
        return date.strftime("%Y-%m-%d")
    elif isinstance(date, datetime.datetime):
        return date.strftime("%Y-%m-%d_%H-%M-%S-%Z")
    elif date is None:
        return "NoDate"
    else:
        raise ValueError(f"Unknown date of type: {type(date)}.")


def _date_from_str(date_str):
    if date_str == "NoDate":
        return None
    elif len(date_str) <= 12:
        return datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    else:
        return datetime.datetime.strptime(date_str, "%Y-%m-%d_%H-%M-%S-%Z")


def check_dataset_integrity(dataset: Dataset, max_count=None, samples: List[Sample] = None, assert_dense=True):
    """Verify the intergrity, coherence and consistancy of a list of a dataset."""

    task_specs = dataset.task_specs
    if samples is None:
        samples = dataset.iter_dataset(max_count=max_count)

    for sample in samples:
        assert len(task_specs.bands_info) == len(sample.band_info_list)
        assert task_specs.n_time_steps == len(sample.dates), f"{task_specs.n_time_steps} vs {len(sample.dates)}"

        for task_band_info, sample_band_info in zip(task_specs.bands_info, sample.band_info_list):
            assert task_band_info == sample_band_info

        shapes = []
        for band in sample.bands:
            band.band_info.assert_valid(band)
            shapes.append(band.data.shape[:2])
        max_shape = np.array(shapes).max(axis=0)
        assert np.all(max_shape == task_specs.patch_size), f"{max_shape} vs {task_specs.patch_size}"

        assert isinstance(task_specs.label_type, LabelType)
        task_specs.label_type.assert_valid(sample.label)

        if assert_dense:
            assert np.all(sample.band_array != None)
