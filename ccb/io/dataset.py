import ast
from multiprocessing.sharedctypes import Value
import pathlib
from attr import attr
import numpy as np
from numpy.lib.function_base import percentile
import rasterio
import json
from pathlib import Path
import datetime
from typing import List, Union, Set, Dict, Tuple
import os
from scipy.ndimage import zoom
import pickle
from functools import cached_property, lru_cache
from warnings import warn
from ccb.io.label import LabelType
from collections import OrderedDict, defaultdict
from tqdm import tqdm
import h5py
from typing import Sequence

# Deprecated, use CCB_DIR instead
src_datasets_dir = os.environ.get("CC_BENCHMARK_SOURCE_DATASETS", os.path.expanduser("~/dataset/"))
datasets_dir = os.environ.get("CC_BENCHMARK_CONVERTED_DATASETS", os.path.expanduser("~/converted_dataset/"))

# src_datasets_dir should now be CCB_DIR / "source" and datasets_dir should be CCB_DIR / "converted"
CCB_DIR = Path(datasets_dir).parent


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

    def __repr__(self):

        return f"BandInfo(name={self.name}, original_res={self.spatial_resolution:.1f}m)"

    def expand_name(self):
        """The name of the band repated with the numbef or channels"""
        return [self.name]


class SpectralBand(BandInfo):
    """Extends BandInfo to provide wavelength of the band."""

    def __init__(self, name=None, alt_names=(), spatial_resolution=None, wavelength=None) -> None:
        super().__init__(name, alt_names, spatial_resolution)
        self.wavelength = wavelength

    def __key(self):
        return (self.name, self.wavelength)

    def __repr__(self):
        return f"{self.__class__.__name__}(name={self.name}, wavelen={self.wavelength}, original_res={self.spatial_resolution:.1f}m)"


class Sentinel1(SpectralBand):
    pass


class Sentinel2(SpectralBand):
    pass


class Landsat8(SpectralBand):
    "Spectral band of type Landsat 8"

    def __repr__(self):
        return "Landsat8(name={}, wavelen={}, original_res={:.1f}m)".format(
            self.name, self.wavelength, self.spatial_resolution
        )


class Mask(BandInfo):
    pass


class ElevationBand(BandInfo):
    pass


class MultiBand(BandInfo):
    """Contains a 3d object with multiple band of the same resolution e.g. HyperSpectralBands"""

    def __init__(self, name=None, alt_names=(), spatial_resolution=None, n_bands=None) -> None:
        super().__init__(name=name, alt_names=alt_names, spatial_resolution=spatial_resolution)
        self.n_bands = n_bands

    def expand_name(self):
        """The name of the band repated with the numbef or channels"""
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
        self._class_names = class_names

    def assert_valid(self, value):
        assert isinstance(value, Band)
        assert value.band_info == self
        assert np.all(value.data >= 0)
        assert np.all(value.data < self.n_classes)

    @property
    def class_names(self):
        if hasattr(self, "_class_names"):
            return self._class_names
        else:
            return self.class_name  # for backward compatibility with saved pickles with a typo

    def __repr__(self) -> str:
        if self.class_names is not None:
            if self.n_classes > 3:
                names = ", ".join(self.class_names[:3]) + "..."
            else:
                names = ", ".join(self.class_names) + "."
        else:
            names = "missing class names"
        return f"{self.n_classes}-SegmentationClasses, {self.spatial_resolution}m resolution ({names})"


sentinel1_8_bands = [
    Sentinel1("01 - VH.Real"),
    Sentinel1("02 - VH.Imaginary"),
    Sentinel1("03 - VV.Real"),
    Sentinel1("04 - VV.Imaginary"),
    Sentinel1("05 - VH.LEE Filtered"),
    Sentinel1("06 - VV.LEE Filtered"),
    Sentinel1("07 - VH.LEE Filtered.Real"),
    Sentinel1("08 - VV.LEE Filtered.Imaginary"),
]


def make_rgb_bands(spatial_resolution):
    return [
        SpectralBand("Red", ("red",), spatial_resolution, 0.665),
        SpectralBand("Green", ("green",), spatial_resolution, 0.56),
        SpectralBand("Blue", ("blue",), spatial_resolution, 0.49),
    ]


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

landsat8_9_bands = [
    Landsat8("01 - Coastal aerosol", ("1", "01", "B1"), 30, 0.443),
    Landsat8("02 - Blue", ("2", "02", "B2", "blue"), 15, 0.482),
    Landsat8("03 - Green", ("3", "03", "B3", "green"), 15, 0.5614),
    Landsat8("04 - Red", ("4", "04", "B4", "red"), 15, 0.6546),
    Landsat8("05 - NIR", ("5", "05", "B5", "nir"), 30, 0.8647),
    Landsat8("06 - SWIR1", ("6", "06", "B6", "swir1"), 30, 1.6089),
    Landsat8("07 - SWIR2", ("7", "07", "B7", "swir2"), 30, 2.2007),
    Landsat8("09 - Cirrus", ("9", "09" "B9", "cirrus"), 30, 1.370),
    Landsat8("10 - Tirs1", ("10", "B10", "tirs1"), 100, 10.9),
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

    def __repr__(self):
        if isinstance(self.data, np.ndarray):
            shape = self.data.shape
        else:
            shape = "unknown"
        return f"Band(info={self.band_info}, shape={shape}, resampled_resolution={self.spatial_resolution}m, date={self.date}, data={self.date})"

    def get_descriptor(self):
        descriptor = self.band_info.name
        if self.date is not None:
            descriptor += f"_{_format_date(self.date)}"
        return descriptor

    def write_to_geotiff(self, directory):
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

        elif data.dtype == np.float64:
            data = data.astype(np.float32)  # see https://github.com/rasterio/rasterio/issues/2384

        file_path = Path(directory, f"{self.get_descriptor()}.tif")
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


def load_band_tif(file_path):
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
    def __init__(self, bands: List[Band], label, sample_name: str) -> None:
        super().__init__()
        self.bands = bands
        self.label = label
        self.sample_name = sample_name
        self._build_index()

    def __repr__(self):
        np.set_printoptions(threshold=5)
        return f"Sample:(name={self.sample_name}, bands=\n{self.bands}\n)"

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

    def write(self, dataset_dir, format="hdf5"):
        writer = dict(hdf5=write_sample_hdf5, tif=write_sample_tif)[format]
        return writer(sample=self, dataset_dir=dataset_dir)


def write_sample_tif(sample, dataset_dir):

    dst_dir = pathlib.Path(dataset_dir, sample.sample_name)
    dst_dir.mkdir(exist_ok=True, parents=True)

    file_set = set()

    # the band index ensure to keep band order and loading by band name.
    band_index = OrderedDict()  # TODO maybe replace this band_index with a structure similar to band array.
    for i, band in enumerate(sample.bands):
        file = band.write_to_geotiff(dst_dir)
        band_name = band.band_info.name
        if band_name not in band_index:
            band_index[band_name] = [file.name]
        else:
            band_index[band_name].append(file.name)
        file_set.add(file)

    if len(file_set) != len(sample.bands):
        raise ValueError("Duplicate band description in bands. Perhaps date is missing?")

    with open(Path(dst_dir, "band_index.json"), "w") as fd:
        json.dump(tuple(band_index.items()), fd)

    if sample.label is not None:
        if isinstance(sample.label, Band):
            if not isinstance(sample.label.band_info, LabelType):
                raise ValueError("The label is of type Band, but its band_info is not instance of Label.")
            sample.label.write_to_geotiff(dst_dir)
        else:
            with open(Path(dst_dir, "label.json"), "w") as fd:
                json.dump(sample.label, fd)


def write_sample_hdf5(sample: Sample, dataset_dir):
    sample_path = Path(dataset_dir) / f"{sample.sample_name}.hdf5"

    with h5py.File(sample_path, "w") as fp:

        attr_dict = {}
        bands = sample.bands

        if sample.label is not None:
            if isinstance(sample.label, Band):
                if not isinstance(sample.label.band_info, LabelType):
                    raise ValueError("The label is of type Band, but its band_info is not instance of Label.")
                bands.append(sample.label)
            else:
                attr_dict["label"] = sample.label

        for band in sample.bands:
            fp.create_dataset(name=band.band_info.name, data=band.data)
            attrs = dict(
                date=band.date,
                date_id=band.date_id,
                spatial_resolution=band.spatial_resolution,
                band_info=band.band_info,
                meta_info=band.meta_info,
            )

            attr_dict[band.band_info.name] = attrs

            # h5_band.attrs["pickle"] = str(pickle.dumps(attrs))

        fp.attrs["pickle"] = str(pickle.dumps(attr_dict))  # seems to be faster to do it in a single one pickle
    return sample_path


def load_sample_hdf5(sample_path: Path, band_names=None, label_only=False):
    with h5py.File(sample_path, "r") as fp:

        attr_dict = pickle.loads(ast.literal_eval(fp.attrs["pickle"]))
        bands = []

        # check if label is present in hdf5 file and retrieve it, or get it from attr_dict
        if "label" in fp.keys():
            label = fp["label"]
            label = Band(data=np.array(label), **attr_dict["label"])
        else:
            label = attr_dict.get("label", None)

        if label_only:
            return Sample(bands=bands, label=label, sample_name=sample_path.stem)
        else:
            for band_name in band_names:

                h5_band = fp[band_name]

                band = Band(data=np.array(h5_band), **attr_dict[band_name])

                bands.append(band)

            return Sample(bands=bands, label=label, sample_name=sample_path.stem)


def load_sample_tif(sample_dir, band_names=None):
    sample_dir = Path(sample_dir)
    band_list = []
    with open(Path(sample_dir, "band_index.json"), "r") as fd:
        band_index = OrderedDict(json.load(fd))

    if band_names is None:
        band_names = band_index.keys()

    for band_name in band_names:
        for file_name in band_index[band_name]:
            band_list.append(load_band_tif(Path(sample_dir, file_name)))

    label_file = Path(sample_dir, "label.json")
    label_file_tif = Path(sample_dir, "label.tif")
    if label_file.exists():
        with open(label_file, "r") as fd:
            label = json.load(fd)
    elif label_file_tif.exists():
        label = load_band_tif(label_file_tif)
    return Sample(band_list, label, sample_name=sample_dir.name)


def load_sample(sample_path: Path, band_names=None, format=None):
    loader = dict(tif=load_sample_tif, hdf5=load_sample_hdf5)[format]
    return loader(sample_path, band_names=band_names)


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


class Partition:
    """Contains a dict mapping 'train', 'valid' 'test' to lists of `sample_name`s."""

    @staticmethod
    def check_split_name(split_name):
        if split_name not in ("train", "valid", "test"):
            raise ValueError(f"split_name must be one of 'train', 'valid', 'test'. Got {split_name}.")

    def __init__(self, partition_dict=None) -> None:
        """If `partition_dict` is None, it will initialize to a dict of empty lists."""
        if partition_dict is None:
            self.partition_dict = {"train": [], "valid": [], "test": []}
        else:
            for key in partition_dict.keys():
                Partition.check_split_name(key)
            self.partition_dict = partition_dict

    def add(self, split_name, sample_name):
        Partition.check_split_name(split_name)
        self.partition_dict[split_name].append(sample_name)

    def save(self, directory, partition_name, as_default=False):
        """
        If as_default is True, create symlink named default_partition.json -> {partition_name}_partition.json
        This will be loaded as the default partition by class Dataset
        """
        file_path = Path(directory, partition_name + "_partition.json")
        with open(file_path, "w") as fd:
            json.dump(self.partition_dict, fd, indent=2)
        if as_default:
            os.symlink(f"{directory}/{partition_name}_partition.json", f"{directory}/default_partition.json")


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
    def __init__(
        self,
        dataset_dir,
        band_names: Sequence[
            str,
        ],
        split=None,
        partition_name="default",
        transform=None,
        format="hdf5",
    ) -> None:
        """
        Load CCB dataset.
        CCB datasets can have different split partitions (e.g. for few-shot learning).
        The default partition is
        Partition

        Args:
            dataset_dir: the path containing the samples of the dataset.
            split: Specify split to use or None for all
            partition_name: Each dataset can have more than 1 partitions. Use this field to specify the active_partition.
            transform: dataset transforms
            band_names: Sequence of band names to select
            format: 'hdf5' or 'tif'
        """
        self.dataset_dir = Path(dataset_dir)
        self.split = split
        self.format = format
        self.transform = transform
        self._load_partitions(partition_name)
        assert split is None or split in self.list_splits(), "Invalid split {}".format(split)
        assert format in ["hdf5", "tif"], f"Invalid file format {format}"
        # define alt names that map alt band names to full band names
        self.alt_band_names = self.alt_to_full_names(band_names)
        # cast user band names to full band names so no need to check for alt names
        self.band_names = [self.alt_band_names[name] for name in band_names]

    def alt_to_full_names(self, band_names) -> Dict[str, str]:
        """Define a dictionary mapping from all alt band names to full band names and check validity.

        Args:
            band_names: band names for which to collect alt names

        Returns:
            dictionary mapping all possible band names to full band name
        """
        bands_info = self.task_specs.bands_info
        alt_band_names = {}
        for user_band_name in band_names:
            matched_band_name = None
            for band_info in bands_info:
                possible_names = band_info.alt_names + (band_info.name,)
                if user_band_name in possible_names:
                    matched_band_name = band_info.name
                    for name in possible_names:
                        alt_band_names[name] = band_info.name

            if matched_band_name is None:
                raise ValueError(
                    f"The band {user_band_name} you specified does not exist in dataset bands {bands_info}."
                )

        return alt_band_names

    #### Loading paths
    def _load_partitions(self, active_partition_name) -> None:
        """
        Scan directory for partition files
        """
        self._partition_path_dict = {}
        for p in self.dataset_dir.glob("*_partition.json"):
            partition_name = p.name.split("_partition.json")[0]
            self._partition_path_dict[partition_name] = p

        self.set_partition(active_partition_name)
        self._sample_name_list = []
        for sample_names in self.active_partition.partition_dict.values():
            self._sample_name_list.extend(sample_names)

        # self._sample_name_list = []
        # for p in self.dataset_dir.glob("*"):  # self.dataset_dir.iterdir():
        #     if p.name.endswith("_partition.json"):
        #         partition_name = p.name.split("_partition.json")[0]
        #         self._partition_path_dict[partition_name] = p
        #     elif p.name == "task_specs.pkl":
        #         self._task_specs_path = p
        #     elif p.is_dir():
        #         self._sample_name_list.append(p.name)

    ### Task specifications
    @cached_property
    def task_specs(self):
        with open(self.dataset_dir / "task_specs.pkl", "rb") as fd:
            task_specs = pickle.load(fd)

        # banchmark name should follow parent dir
        task_specs.benchmark_name = self.dataset_dir.parent.name
        return task_specs

    #### Splits ####

    def set_split(self, split):
        assert split is None or split in self.list_splits()
        self.split = split

    def list_splits(self):
        """
        List splits for active partition
        """
        return list(self.active_partition.partition_dict.keys())

    #### Partitions ####

    def set_partition(self, partition_name="default"):
        """
        Select active partition by name
        """

        if partition_name not in self._partition_path_dict:
            raise ValueError(
                f"Unknown partition {partition_name}. Maybe the dataset is missing a default_partition.json?"
            )
        self.active_partition_name = partition_name
        self.active_partition = self.load_partition(partition_name)

    def list_partitions(self):
        return list(self._partition_path_dict.keys())

    @lru_cache(maxsize=3)
    def load_partition(self, partition_name="default") -> Partition:
        """
        Load and return partition content from json file
        """
        with open(self._partition_path_dict[partition_name], "r") as fd:
            return Partition(json.load(fd))

    def _load_partition(self, partition_name):
        """
        Maybe this logic can be improved???

        Current:
        -> If "default" partition does not exist, load original parition, and if that one does not exist, load any
        -> If "default" partition exists, then load partition_name.

        Proposed:
        -> Load partition_name. If it doesn't exist, raise Exception.
        -> Always provide a default_partition.json (job of converter)
        -> Don't consider original_partition.json to be a special case
        -> Use "default" as default parameter for partition_name in __init__
        """
        if len(self._partition_path_dict) == 0:
            warn(f"No partition found for dataset {self.dataset_dir.name}.")
            return

        if "default" not in self._partition_path_dict:
            partition_name = None
            if "original" in self._partition_path_dict:
                partition_name = "original"
            else:
                partition_name = self._partition_path_dict.keys()[0]  # take any partition??

            self._partition_path_dict["default"] = self._partition_path_dict[partition_name]
            warn(f"No default partition found for dataset {self.dataset_dir.name}. Using {partition_name} as default.")

        self.set_partition(partition_name)

    @cached_property
    def band_stats(self):
        with open(self.dataset_dir / "band_stats.json", "r") as fd:
            all_band_stats_dict = json.load(fd)
        band_stats = {}
        for band_name, stats_dict in all_band_stats_dict.items():
            band_stats[band_name] = Stats(**stats_dict)
        return band_stats

    def rgb_stats(self):
        try:
            blue = self.band_stats["02 - Blue"]
            green = self.band_stats["03 - Green"]
            red = self.band_stats["04 - Red"]
        except KeyError:
            blue = self.band_stats["Blue"]
            green = self.band_stats["Green"]
            red = self.band_stats["Red"]
        return (red.mean, green.mean, blue.mean), (red.std, green.std, blue.std)

    def normalization_stats(
        self,
    ) -> Tuple[Tuple[float, ...]]:
        """Retrieve band mean and std statistics for image normalization for dataset bands."""
        means = []
        stds = []
        for band_name in self.band_names:
            band_stat = self.band_stats[band_name]
            means.append(band_stat.mean)
            stds.append(band_stat.std)

        return tuple(means), tuple(stds)

    #### Common accessors and iterators ####

    def __getitem__(self, idx):
        """
        Return item idx from active split, from active partition
        """
        if self.split is None:
            sample_name_list = self._sample_name_list
        else:
            sample_name_list = self.active_partition.partition_dict[self.split]
        sample_name = sample_name_list[idx]
        if self.format == "hdf5":
            sample_name += ".hdf5"
        sample = load_sample(Path(self.dataset_dir, sample_name), band_names=self.band_names, format=self.format)
        if self.transform is not None:
            return self.transform(sample)
        else:
            return sample

    def __len__(self):
        """
        Return length of active split, from active partition
        """
        if self.split is None:
            sample_name_list = self._sample_name_list
        else:
            sample_name_list = self.active_partition.partition_dict[self.split]
        return len(sample_name_list)

    def _iter_dataset(self, max_count=None):
        indexes = np.random.choice(len(self), size=max_count, replace=False)
        for idx in indexes:
            yield self[idx]
        # if self.split is None:
        #     sample_name_list = self._sample_name_list
        # else:
        #     sample_name_list = self.active_partition.partition_dict[self.split]
        # sample_names = np.random.choice(sample_name_list, size=max_count, replace=False)
        # for sample_name in sample_names:
        #     yield load_sample(Path(self.dataset_dir, sample_name))

    def iter_dataset(self, max_count=None):
        n = len(self)
        if max_count is None:
            max_count = n
        else:
            max_count = min(n, max_count)

        return GeneratorWithLength(self._iter_dataset(max_count=max_count), max_count)

    #### len and printing utils ####
    def get_available_stats_str(self):
        """
        String for visualizing which stats are available
        (used for __repr__ and __str__)
        """
        which_stats = []
        for partition in self.stats:
            if partition == "all":
                which_stats.append("all")
            else:
                for split in self.stats[partition]:
                    which_stats.append(f"{partition}:{split}")
        if which_stats:
            return "|".join(which_stats)
        else:
            return "<N/A>"

    def __repr__(self):
        return f"Dataset(dataset_dir={ self.dataset_dir}, split={self.split}, active_partition={self.active_partition_name}, n_samples={len(self)})"


class Stats:
    def __init__(
        self,
        min,
        max,
        mean,
        std,
        median,
        percentile_0_1,
        percentile_1,
        percentile_5,
        percentile_95,
        percentile_99,
        percentile_99_9,
    ) -> None:
        # Convert all to float to avoid serialization issues with int16
        self.min = float(min)
        self.max = float(max)
        self.mean = float(mean)
        self.std = float(std)
        self.median = float(median)
        self.percentile_0_1 = float(percentile_0_1)
        self.percentile_1 = float(percentile_1)
        self.percentile_5 = float(percentile_5)
        self.percentile_95 = float(percentile_95)
        self.percentile_99 = float(percentile_99)
        self.percentile_99_9 = float(percentile_99_9)

    def to_dict(self):
        return self.__dict__


def compute_stats(values):
    q_0_1, q_1, q_5, median, q_95, q_99, q_99_9 = np.percentile(values, q=[0.1, 1, 5, 50, 95, 99, 99.9])
    stats = Stats(
        min=np.min(values),
        max=np.max(values),
        mean=np.mean(values),
        std=np.std(values),
        median=median,
        percentile_0_1=q_0_1,
        percentile_1=q_1,
        percentile_5=q_5,
        percentile_95=q_95,
        percentile_99=q_99,
        percentile_99_9=q_99_9,
    )
    return stats


def compute_dataset_statistics(dataset, n_value_per_image=1000, n_samples=None):

    accumulator = defaultdict(list)
    if n_samples is not None and n_samples < len(dataset):
        indices = np.random.choice(len(dataset), n_samples, replace=False)
    else:
        indices = list(range(len(dataset)))

    for i in tqdm(indices, desc="Extracting Statistics"):
        sample = dataset[i]

        for band in sample.bands:
            if n_value_per_image is None:
                accumulator[band.band_info.name].append(band.data.flatten())
            else:
                accumulator[band.band_info.name].append(
                    np.random.choice(band.data.flat, size=n_value_per_image, replace=False)
                )

        if isinstance(sample.label, Band):
            if n_value_per_image is None:
                accumulator["label"].append(sample.label.data.flatten())
            else:
                accumulator["label"].append(
                    np.random.choice(sample.label.data.flat, size=n_value_per_image, replace=False)
                )
        elif isinstance(sample.label, (list, tuple)):
            for obj in sample.label:
                if isinstance(obj, dict):
                    for key, val in obj.items():
                        accumulator[f"label_{key}"].append(val)
        else:
            accumulator["label"].append(sample.label)

    band_values = {}
    band_stats = {}
    for name, values in accumulator.items():
        values = np.hstack(values)
        band_values[name] = values
        band_stats[name] = compute_stats(values)

    return band_values, band_stats


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

    for partition_name in dataset._partition_path_dict.keys():
        print(f"check integrity of {partition_name}")
        partition = dataset.load_partition(partition_name)
        check_partition_integrity(partition, partition_name)

    task_specs = dataset.task_specs
    if samples is None:
        samples = dataset.iter_dataset(max_count=max_count)

    for sample in samples:
        assert len(task_specs.bands_info) == len(sample.band_info_list)
        # assert task_specs.n_time_steps == len(sample.dates), f"{task_specs.n_time_steps} vs {len(sample.dates)}"  # forestnet couldn't pass this test.

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
            assert np.all(sample.band_array is not None)


def check_partition_integrity(partition: Partition, partition_name: str):
    all_names = []
    for split, names in partition.partition_dict.items():
        if len(names) == 0:
            warn(f"{split} of {partition_name} is empty.")
        assert len(set(names)) == len(names), f"Non unique names in split {split}."
        all_names.extend(names)

    assert len(set(all_names)) == len(all_names), "Overlap between the different subsets."
