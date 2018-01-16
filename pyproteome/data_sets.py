"""
This module provides functionality for manipulating proteomics data sets.

Functionality includes merging data sets and interfacing with attributes in a
structured format.
"""

# Built-ins
from __future__ import absolute_import, division

from collections import OrderedDict
import copy
import logging
import os
import warnings
import sys
from functools import partial

# Core data analysis libraries
import pandas as pd
import numpy as np
import numpy.ma as ma
from scipy.stats import ttest_ind

from . import levels, loading, modification, utils
from .motifs import motif as pymotif


LOGGER = logging.getLogger("pyproteome.data_sets")


class DataSet:
    """
    Class that encompasses a proteomics data set.

    Includes peptide list, scan list, channels, groups, quantitative
    phenotypes, etc.

    Attributes
    ----------
    mascot_name : str, optional
    psms : :class:`pandas.DataFrame`
        Contains at least "Proteins", "Sequence", and "Modifications" columns.
    channels : dict of str, str
        Maps label channel to sample name.
    groups : dict of str, list of str
        Maps groups to list of sample names. The primary group is considered
        as the first in this sequence.
    phenotypes : dict of str, (dict of str, float)
        Primary key is the phenotype and its value is a dictionary mapping
        sample names to that phenotype's value.
    name : str
    levels : dict or str, float, optional
    sets : int
        Number of sets merged into this data set.
    sources : list of str
    scan_list : dict of str, list of int
    """
    def __init__(
        self,
        mascot_name=None,
        channels=None,
        psms=None,
        groups=None,
        phenotypes=None,
        name="",
        lvls=None,
        dropna=False,
        pick_best_ptm=True,
        merge_duplicates=True,
        merge_subsets=False,
        filter_bad=True,
    ):
        """
        Initializes a data set.

        Parameters
        ----------
        channels : dict of (str, str), optional
            Ordered dictionary mapping sample names to quantification channels
            (i.e. {"X": "126", "Y": "127", "Z": "128", "W": "129"})
        psms : :class:`pandas.DataFrame`, optional
            Read psms directly from a DataFrame object.
        mascot_name : str, optional
            Read psms from MASCOT / Discoverer data files.
        groups : dict of str, list of str, optional
            Ordered dictionary mapping sample names to larger groups
            (i.e. {"WT": ["X", "Y"], "Diseased": ["W", "Z"]})
        phenotypes : dict of str, (dict of str, float), optional
        name : str, optional
        lvls : dict or str, float, optional
        dropna : bool, optional
            Drop scans that have any channels with missing quantification
            values.
        pick_best_ptm : bool, optional
            Select the peptide sequence for each scan that has the highest
            MASCOT ion score. (i.e. ["pSTY": 5, "SpTY": 10, "STpY": 20] =>
            "STpY")
        merge_duplicates : bool, optional
            Merge scans that have the same peptide sequence into one peptide,
            summing the quantification channel intensities to give a weighted
            estimate of relative abundances.
        merge_subsets : bool, optional
            Merge peptides that are subsets of one another (i.e. "RLK" => "LK")
        filter_bad : bool or dict, optional
            Remove peptides that do not have a "High" confidence score from
            ProteomeDiscoverer.
        """
        if mascot_name and os.path.splitext(mascot_name)[1] == "":
            mascot_name += ".msf"

        if filter_bad is True:
            filter_bad = dict(
                ion_score=15,
                isolation=50,
                median_quant=1000,
            )

        self.channels = channels or OrderedDict()
        self.groups = groups or OrderedDict()
        self.sources = ["unknown"]
        self.validated = False
        self.group_a, self.group_b = None, None

        if mascot_name:
            psms, lst = loading.load_mascot_psms(
                mascot_name,
                pick_best_ptm=pick_best_ptm,
            )
            self.sources = ["MASCOT"]

        if psms is None:
            psms = pd.DataFrame(
                columns=[
                    "Proteins",
                    "Sequence",
                    "Modifications",
                    "Validated",
                    "First Scan",
                    "Confidence Level",
                    "Ion Score",
                    "Isolation Interference",
                    "Scan Paths",
                    "Missed Cleavages",
                ] + list(self.channels.values()),
            )

        self.psms = psms
        self.phenotypes = phenotypes
        self.name = name
        self.levels = lvls
        self.mascot_name = mascot_name

        self.intra_normalized = False
        self.sets = 1

        if dropna:
            LOGGER.info("Dropping channels with NaN values.")
            self.dropna(inplace=True)

        if filter_bad:
            LOGGER.info("Filtering peptides that Discoverer marked as bad.")
            self.filter(
                filter_bad,
                inplace=True,
            )

        if pick_best_ptm and (
            not mascot_name or
            os.path.splitext(mascot_name)[1] != ".msf" or
            any(i for i in lst)
        ):
            LOGGER.info("Picking peptides with best ion score for each scan.")
            self._pick_best_ptm()

        if merge_duplicates:
            LOGGER.info("Merging duplicate peptide hits together.")
            self._merge_duplicates()

        if merge_subsets:
            LOGGER.info("Merging peptide hits that are subsets together.")
            self._merge_subsequences()

        self.update_group_changes()

    def copy(self):
        """
        Make a copy of self.

        Returns
        -------
        :class:`DataSet<pyproteome.data_sets.DataSet>`
        """
        new = copy.copy(self)

        new.psms = new.psms.copy()
        new.channels = new.channels.copy()
        new.groups = new.groups.copy()

        return new

    @property
    def samples(self):
        return list(self.channels.keys())

    def __str__(self):
        return (
            "<pyproteome.DataSet object" +
            (
                ": " + self.name
                if self.name else
                " at " + hex(id(self))
            ) +
            ">"
        )

    def __getitem__(self, key):
        if isinstance(key, slice):
            new = self.copy()
            new.psms = new.psms[key]
            return new

        if any(
            isinstance(key, i)
            for i in [str, list, tuple, pd.Series, np.ndarray]
        ):
            return self.psms[key]

        raise TypeError(type(key))

    def _pick_best_ptm(self):
        reject_mask = np.zeros(self.psms.shape[0], dtype=bool)

        for index, row in self.psms.iterrows():
            hits = np.logical_and(
                self.psms["First Scan"] == row["First Scan"],
                self.psms["Sequence"] != row["Sequence"],
            )

            if "Rank" in self.psms.columns:
                better = self.psms["Rank"] < row["Rank"]
            else:
                better = self.psms["Ion Score"] > row["Ion Score"]

            hits = np.logical_and(hits, better)

            if hits.any():
                reject_mask[index] = True

        self.psms = self.psms[~reject_mask].reset_index(drop=True)

    def _merge_duplicates(self):
        if len(self.psms) < 1:
            return

        channels = list(self.channels.values())
        agg_dict = {}

        for channel in channels:
            weight = "{}_weight".format(channel)

            if weight in self.psms.columns:
                self.psms[channel] *= self.psms[weight]
                agg_dict[weight] = _nan_sum

            agg_dict[channel] = _nan_sum

        def _first(x):
            if not all(i == x.values[0] for i in x.values):
                LOGGER.warning(
                    "Mismatch between peptide data: '{}' not in {}".format(
                        x.values[0],
                        [str(i) for i in x.values[1:]],
                    )
                )

            return x.values[0]

        agg_dict["Proteins"] = _first
        agg_dict["Modifications"] = _first
        agg_dict["Missed Cleavages"] = _first
        agg_dict["Validated"] = all
        agg_dict["Scan Paths"] = utils.flatten_set
        agg_dict["First Scan"] = utils.flatten_set
        agg_dict["Ion Score"] = max
        agg_dict["Confidence Level"] = partial(
            max,
            key=lambda x: ["Low", "Medium", "High"].index(x),
        )
        agg_dict["Isolation Interference"] = min

        self.psms = self.psms.groupby(
            by=[
                "Sequence",
            ],
            sort=False,
            as_index=False,
        ).agg(agg_dict)

        for channel in channels:
            weight = "{}_weight".format(channel)

            if weight in self.psms.columns:
                self.psms[channel] = (
                    self.psms[channel] / self.psms[weight]
                )

    def _merge_subsequences(self):
        """
        Merges petides that are a subsequence of another peptide.

        Only merges peptides that contain the same set of modifications and
        that map to the same protein(s).
        """
        psms = self.psms

        # Find all proteins that have more than one peptide mapping to them
        for index, row in psms[
            psms.duplicated(subset="Proteins", keep=False)
        ].iterrows():
            seq = row["Sequence"]

            # Then iterate over each peptide and find other non-identical
            # peptides that map to the same protein
            for o_index, o_row in psms[
                np.logical_and(
                    psms["Proteins"] == row["Proteins"],
                    psms["Sequence"] != seq,
                )
            ].iterrows():
                # If that other peptide is a subset of this peptide, rename it
                if o_row["Sequence"] in seq:
                    cols = [
                        "Sequence",
                        "Modifications",
                        "Missed Cleavages",
                    ]
                    psms.at[o_index, cols] = row[cols]

        # And finally group together peptides that were renamed
        self._merge_duplicates()

    def __add__(self, other):
        """
        Concatenate two data sets.

        Combines two data sets, adding together the channel values for any
        common data.

        Parameters
        ----------
        other : :class:`DataSet<pyproteome.data_sets.DataSet>`

        Returns
        -------
        :class:`DataSet<pyproteome.data_sets.DataSet>`
        """
        return merge_data([self, other])

    def rename_channels(self):
        """
        Rename all channels from quantification channel name to sample name.
        (i.e. "126" => "Mouse 1337")
        """
        for new_channel, old_channel in self.channels.items():
            if new_channel != old_channel:
                new_weight = "{}_weight".format(new_channel)

                if (
                    new_channel in self.psms.columns or
                    new_weight in self.psms.columns
                ):
                    raise Exception(
                        "Channel {} already exists, cannot rename to it"
                        .format(new_channel)
                    )

                self.psms[new_channel] = self.psms[old_channel]
                del self.psms[old_channel]

                old_weight = "{}_weight".format(old_channel)

                if old_weight in self.psms.columns:
                    self.psms[new_weight] = self.psms[old_weight]
                    del self.psms[old_weight]

        self.channels = OrderedDict([
            (key, key) for key in self.channels.keys()
        ])

    def inter_normalize(self, norm_channels=None, other=None, inplace=False):
        """
        Normalize runs to one channel for inter-run comparions.

        Parameters
        ----------
        norm_channels : list of str, optional
        other : :class:`DataSet<pyproteome.data_sets.DataSet>`, optional
        inplace : bool, optional
            Modify this data set in place.

        Returns
        -------
        :class:`DataSet<pyproteome.data_sets.DataSet>`
        """
        assert (
            norm_channels is not None or
            other is not None
        )

        new = self

        if not inplace:
            new = new.copy()

        new.rename_channels()

        if norm_channels is None:
            norm_channels = set(new.channels).intersection(other.channels)

        if len(norm_channels) == 0:
            return new

        # Filter norm channels to include only those in other data set
        norm_channels = [
            chan
            for chan in norm_channels
            if not other or chan in other.channels
        ]
        for channel in new.channels.values():
            weight = "{}_weight".format(channel)

            if weight not in new.psms.columns:
                new.psms[weight] = (
                    new.psms[channel] *
                    (100 - new.psms["Isolation Interference"]) / 100
                )

        # Calculate the mean normalization signal from each shared channel
        new_mean = new.psms[norm_channels].mean(axis=1)

        # Drop values for which there is no normalization data
        new.psms = new.psms[~new_mean.isnull()].reset_index(drop=True)

        if other:
            merge = pd.merge(
                new.psms,
                other.psms,
                on=[
                    "Proteins",
                    "Sequence",
                    "Modifications",
                ],
                how="left",
                suffixes=("_self", "_other"),
            )
            assert merge.shape[0] == new.psms.shape[0]

            self_mean = merge[
                ["{}_self".format(i) for i in norm_channels]
            ].mean(axis=1)

            other_mean = merge[
                ["{}_other".format(i) for i in norm_channels]
            ].mean(axis=1)

            for channel in new.channels.values():
                vals = merge[
                    channel
                    if channel in merge.columns else
                    "{}_self".format(channel)
                ]

                # Set scaling factor to 1 where other_mean is None
                cp = other_mean.copy()
                cp[other_mean.isnull()] = (
                    self_mean[other_mean.isnull()]
                )

                if self_mean.any():
                    vals *= cp / self_mean

                assert new.psms.shape[0] == vals.shape[0]
                new.psms[channel] = vals

        new.update_group_changes()

        return new

    def normalize(self, lvls, inplace=False):
        """
        Normalize channels to given levels for intra-run comparisons.

        Divides all channel values by a given level.

        Parameters
        ----------
        lvls : dict of str, float or
        :class:`DataSet<pyproteome.data_sets.DataSet>`
            Mapping of channel names to normalized levels. Alternatively,
            a data set to pass to levels.get_channel_levels() or use
            pre-calculated levels from.
        inplace : bool, optional
            Modify this data set in place.

        Returns
        -------
        :class:`DataSet<pyproteome.data_sets.DataSet>`
        """
        new = self

        # Don't normalize a data set twice!
        assert not new.intra_normalized

        if not inplace:
            new = new.copy()

        if isinstance(lvls, DataSet):
            if not lvls.levels:
                lvls.levels = levels.get_channel_levels(lvls)

            lvls = lvls.levels

        new_channels = utils.norm(self.channels)

        for key, norm_key in zip(
            self.channels.values(),
            new_channels.values(),
        ):
            new.psms[norm_key] = new.psms[key] / lvls[key]
            del new.psms[key]

        new.intra_normalized = True
        new.channels = new_channels
        new.groups = self.groups.copy()

        new.update_group_changes()

        return new

    def dropna(self, how="any", groups=None, inplace=False):
        """
        Drop any channels with NaN values.

        Parameters
        ----------
        how : str, optional
        groups : list of str, optional
            Only drop rows with NaN in columns within groups.
        inplace : bool, optional

        Returns
        -------
        :class:`DataSet<pyproteome.data_sets.DataSet>`
        """
        new = self

        if not inplace:
            new = new.copy()

        if groups is None:
            groups = list(new.channels.values())

        columns = [
            new.channels[chan]
            for group in groups
            for chan in new.groups[group]
            if chan in new.channels
        ]

        new.psms = new.psms.dropna(
            axis=0,
            how=how,
            subset=columns,
        ).reset_index(drop=True)

        return new

    def add_peptide(self, insert):
        defaults = {
            "Validated": False,
            "First Scan": set(),
            "Scan Paths": set(),
            "Ion Score": 100,
            "Isolation Interference": 0,
            "Missed Cleavages": 0,
            "Confidence Level": "High",
        }

        for key, val in defaults.items():
            if key not in insert:
                insert[key] = val

        self.psms = self.psms.append(pd.Series(insert), ignore_index=True)

    def filter(
        self,
        filters=None,
        inplace=False,
        **kwargs
    ):
        """
        Filters a data set.

        Parameters
        ----------
        filters : list of dict or dict, optional
            List of filters to apply to data set. Filters are also pulled from
            **kwargs (see below).
        inplace : bool, optional
            Perform the filter on self, other create a copy and return the
            new object.

        Notes
        -----
        These parameters filter your data set to only include peptides that
        match a given attribute. For example:

            # Filter for all peptides with p-value < 0.01, and a fold change
            # greater than 2x or less than 0.5x
            >>> data.filter(p=0.01, fold=2)

        This function interprets both the argument filter and python's **kwargs
        magic. The three functions are all equivalent:

            >>> data.filter(p=0.01)
            >>> data.filter([{"p": 0.01}])
            >>> data.filter({"p": 0.01})

        Filter parameters can be one of any below:

        ================    ===================================================
        Name                Description
        ================    ===================================================
        seies               Use a pandas series (data.psms[series]).
        fn                  Use data.psms.apply(fn).
        group_a             Calculate p / fold change values from group_a.
        group_b             Calculate p / fold change values from group_b.
        confidence          Discoverer's peptide confidence (High|Medium|Low).
        ion_score           MASCOT's ion score.
        isolation           Discoverer's isolation inference.
        missed_cleavage     Missed cleaves <= cutoff.
        median_quant        Median quantification signal > cutoff.
        p                   p-value < cutoff.
        asym_fold           Change > val if cutoff > 1 else Change < val.
        fold                Change > cutoff or Change < 1 / cutoff.
        motif               Filter for motif.
        protein             Filter for protein or list of proteins.
        sequence            Filter for sequence or list of sequences.
        mod_types           Filter for modifications.
        only_validated      Use rows validated by CAMV.
        inverse             Use all rows that are rejected by a filter.
        ================    ===================================================

        Returns
        -------
        :class:`DataSet<pyproteome.data_sets.DataSet>`
        """
        new = self

        if filters is None:
            filters = []

        if filters and not isinstance(filters, (list, tuple)):
            filters = [filters]

        if kwargs:
            filters += [kwargs]

        if not inplace:
            new = new.copy()

        confidence = {
            "High": ["High"],
            "Medium": ["Medium", "High"],
            "Low": ["Low", "Medium", "High"],
        }

        fns = {
            "series": lambda val, psms:
            val,

            "fn": lambda val, psms:
            psms.apply(val, axis=1),

            "confidence": lambda val, psms:
            psms["Confidence Level"].isin(confidence[val]),

            "ion_score": lambda val, psms:
            psms["Ion Score"] >= val,

            "isolation": lambda val, psms:
            psms["Isolation Interference"] <= val,

            "missed_cleavage": lambda val, psms:
            psms["Missed Cleavages"] <= val,

            "median_quant": lambda val, psms:
            np.nan_to_num(
                np.nanmedian(
                    psms[
                        [chan for chan in new.channels.values()]
                    ],
                    axis=1,
                )
            ) >= val,

            "p": lambda val, psms:
            ~psms["p-value"].isnull() &
            (psms["p-value"] <= val),

            "asym_fold": lambda val, psms:
            ~psms["Fold Change"].isnull() &
            (
                psms["Fold Change"] >= val
                if val > 1 else
                psms["Fold Change"] <= val
            ),

            "fold": lambda val, psms:
            ~psms["Fold Change"].isnull() &
            (
                psms["Fold Change"].apply(
                    lambda x:
                    x if x > 1 else (1 / x if x else x)
                ) >= (val if val > 1 else 1 / val)
            ),

            "motif": lambda val, psms:
            psms["Sequence"].apply(
                lambda x:
                any(
                    val.match(nmer)
                    for nmer in pymotif.generate_n_mers(
                        x,
                        letter_mod_types=f.get("mod_types", None),
                    )
                )
            ),

            "protein": lambda val, psms:
            psms["Proteins"].apply(
                lambda x: bool(set(val).intersection(x.genes))
            )
            if isinstance(val, (list, tuple, pd.Series)) else
            psms["Proteins"] == val,

            "sequence": lambda val, psms:
            psms["Sequence"].apply(lambda x: any(i in x for i in val))
            if isinstance(val, (list, tuple, pd.Series)) else
            psms["Sequence"] == val,

            "mod_types": lambda val, psms:
            modification.filter_mod_types(psms, val),

            "only_validated": lambda val, psms:

            psms["Validated"] == val,

            "scan_paths": lambda val, psms:
            psms["Scan Paths"]
            .apply(lambda x: any(i in val for i in x))
        }

        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore',
                r'All-NaN (slice|axis) encountered',
            )

            for f in filters:
                group_a = f.pop("group_a", None)
                group_b = f.pop("group_b", None)

                if group_a or group_b:
                    new.update_group_changes(
                        group_a=group_a,
                        group_b=group_b,
                    )

                inverse = f.pop("inverse", False)

                for key, val in f.items():
                    mask = fns[key](val, new.psms)

                    if inverse:
                        mask = ~mask

                    assert mask.shape[0] == new.psms.shape[0]

                    new.psms = new.psms.loc[mask].reset_index(drop=True)

        new.psms.reset_index(inplace=True, drop=True)

        return new

    def get_groups(self, group_a=None, group_b=None):
        """
        Get channels associated with two groups.

        Parameters
        ----------
        group_a : str or list of str, optional
        group_b : str or list of str, optional

        Returns
        -------
        samples : list of str
        labels : list of str
        groups : tuple of (str or list of str)
        """
        groups = [
            val
            for key, val in self.groups.items()
            if any(chan in self.channels for chan in val)
        ]
        labels = [
            key
            for key, val in self.groups.items()
            if any(chan in self.channels for chan in val)
        ]

        group_a = group_a or self.group_a
        group_b = group_b or self.group_b

        if group_a is None:
            label_a = labels[0] if labels else None
            samples_a = groups[0] if groups else []
        elif isinstance(group_a, str):
            label_a = group_a
            samples_a = self.groups[group_a]
        else:
            group_a = [
                group
                for group in group_a
                if any(
                    sample in self.channels
                    for sample in self.groups[group]
                )
            ]
            label_a = ", ".join(group_a)
            samples_a = [
                sample
                for i in group_a
                for sample in self.groups[i]
                if sample in self.channels
            ]

        if group_b is None:
            label_b = labels[1] if labels[1:] else None
            samples_b = groups[1] if groups[1:] else []
        elif isinstance(group_b, str):
            label_b = group_b
            samples_b = self.groups[group_b]
        else:
            group_b = [
                group
                for group in group_b
                if any(
                    sample in self.channels
                    for sample in self.groups[group]
                )
            ]
            label_b = ", ".join(group_b)
            samples_b = [
                sample
                for i in group_b
                for sample in self.groups[i]
                if sample in self.channels
            ]

        return (samples_a, samples_b), (label_a, label_b), (group_a, group_b)

    def update_group_changes(self, group_a=None, group_b=None):
        """
        Update a table's Fold-Change, and p-value columns.

        Values are calculated based on changes between group_a and group_b.

        Parameters
        ----------
        psms : :class:`pandas.DataFrame`
        group_a : str or list of str, optional
        group_b : str or list of str, optional
        """
        if self.psms.shape[0] < 1:
            return

        (group_a, group_b), _, (self.group_a, self.group_b) = self.get_groups(
            group_a=group_a,
            group_b=group_b,
        )

        channels_a = [
            self.channels[i]
            for i in group_a
            if i in self.channels
        ]

        channels_b = [
            self.channels[i]
            for i in group_b
            if i in self.channels
        ]

        if channels_a and channels_b:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)
                self.psms["Fold Change"] = pd.Series(
                    np.nanmean(self.psms[channels_a], axis=1) /
                    np.nanmean(self.psms[channels_b], axis=1),
                    index=self.psms.index,
                )

                pvals = ttest_ind(
                    self.psms[channels_a],
                    self.psms[channels_b],
                    axis=1,
                    nan_policy="omit",
                )[1]

                if ma.is_masked(pvals):
                    pvals = ma.fix_invalid(pvals, fill_value=np.nan)
                elif pvals.shape == ():
                    pvals = [pvals]

                self.psms["p-value"] = pd.Series(pvals, index=self.psms.index)
        else:
            self.psms["Fold Change"] = np.nan
            self.psms["p-value"] = np.nan

    def print_stats(self, out=sys.stdout):
        data = self.dropna(how="all")

        data_p = self.filter(mod_types=[(None, "Phospho")])
        data_pst = self.filter(mod_types=[("S", "Phospho"), ("T", "Phospho")])
        data_py = self.filter(mod_types=[("Y", "Phospho")])

        out.write(
            "{}\n{} pY, {} pST ({:.0%} Specificity)\n{} total, {} proteins\n"
            .format(
                self.name,
                len(data_py.psms),
                len(data_pst.psms),
                len(data_p.psms) / len(self.psms),
                len(data.psms),
                len(data.genes),
            )
        )

    @property
    def genes(self):
        return sorted(
            set(
                gene
                for i in self.psms["Proteins"]
                for gene in i.genes
            )
        )

    @property
    def data(self):
        """
        Get the raw data corresponding to each channels' intensities for each
        peptide.

        Returns
        -------
        :class:`pandas.DataFrame`
        """
        return self.psms[
            [
                self.channels[chan]
                for group in self.groups.values()
                for chan in group
                if chan in self.channels
            ]
        ]


def merge_data(
    data_sets, name=None, norm_channels=None,
    merge_duplicates=True, merge_subsets=False,
):
    """
    Merge a list of data sets together.

    Parameters
    ----------
    data_sets : list of :class:`DataSet<pyproteome.data_sets.DataSet>`
    name : str, optional
    merge_duplicates : bool, optional
    merge_subsets : bool, optional

    Returns
    -------
    :class:`DataSet<pyproteome.data_sets.DataSet>`
    """
    new = DataSet(
        name=name,
    )

    if len(data_sets) < 1:
        return new

    # if any(not isinstance(data, DataSet) for data in data_sets):
    #     raise TypeError(
    #         "Incompatible types: {}".format(
    #             [type(data) for data in data_sets]
    #         )
    #     )

    for index, data in enumerate(data_sets):
        # Update new.groups
        for group, samples in data.groups.items():
            if group not in new.groups:
                new.groups[group] = samples
                continue

            new.groups[group] += [
                sample
                for sample in samples
                if sample not in new.groups[group]
            ]

        # Normalize data sets to their common channels
        if len(data_sets) > 1:
            data = data.inter_normalize(
                other=new if index > 0 else None,
                norm_channels=(
                    norm_channels
                    if norm_channels else (
                        set(data.channels).intersection(new.channels)
                        if index > 0 else
                        set(data.channels).intersection(data_sets[1].channels)
                    )
                ),
            )

        for key, val in data.channels.items():
            assert new.channels.get(key, val) == val

            if key not in new.channels:
                new.channels[key] = val

        new.psms = pd.concat([new.psms, data.psms])

        if merge_duplicates:
            new._merge_duplicates()

    if merge_subsets:
        new._merge_subsequences()

    new.sources = sorted(
        set(
            source
            for data in data_sets
            for source in data.sources
        )
    )
    new.sets = sum(data.sets for data in data_sets)

    new.group_a = next(
        (i.group_a for i in data_sets if i.group_a is not None),
        None,
    )
    new.group_b = next(
        (i.group_b for i in data_sets if i.group_b is not None),
        None,
    )

    new.update_group_changes()

    if name:
        new.name = name

    return new


def _nan_sum(lst):
    if all(np.isnan(i) for i in lst):
        return np.nan
    else:
        return np.nansum(lst)
