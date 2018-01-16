
from __future__ import absolute_import, division

import os
import pickle

import pyproteome as pyp


def _make_folder(data, folder_name=None):
    if folder_name is None:
        folder_name = os.path.join(data.name, "Clusters")

    return pyp.utils.makedirs(folder_name)


def auto_clusterer(
    data,
    get_data_kwargs=None,
    cluster_kwargs=None,
    cluster_cluster_kwargs=None,
    volcano_kwargs=None,
    folder_name=None,
    filename="clusters.pkl",
):
    """
    Cluster and generate plots for a data set.

    Parameters
    ----------
    data : :class:`DataSet<pyproteome.data_sets.DataSet>`
    """
    folder_name = _make_folder(data, folder_name=folder_name)

    get_data_kwargs = get_data_kwargs or {}
    cluster_kwargs = cluster_kwargs or {}
    cluster_cluster_kwargs = cluster_cluster_kwargs or {}
    volcano_kwargs = volcano_kwargs or {}

    data = pyp.cluster.get_data(
        data,
        **get_data_kwargs
    )

    pyp.cluster.plot.pca(data)

    if "n_clusters" not in cluster_kwargs:
        cluster_kwargs["n_clusters"] = 100

    _, y_pred_old = pyp.cluster.cluster(
        data,
        **cluster_kwargs
    )

    y_pred = pyp.cluster.cluster_clusters(
        data, y_pred_old,
        **cluster_cluster_kwargs
    )

    pyp.cluster.plot.cluster_corrmap(
        data, y_pred_old,
        filename="First Clusters.png",
    )
    pyp.cluster.plot.cluster_corrmap(
        data, y_pred,
        filename="Final Clusters.png",
    )

    pyp.cluster.plot.plot_all_clusters(data, y_pred)

    ss = sorted(set(y_pred))

    for ind in ss:
        pyp.volcano.plot_volcano_filtered(
            data["ds"], {"series": y_pred == ind},
            title="Cluster {}".format(ind),
            folder_name=folder_name,
            **volcano_kwargs
        )

        f, _ = pyp.motifs.logo.make_logo(
            data["ds"], {"series": y_pred == ind},
            title="Cluster {}".format(ind),
        )

        if f:
            f.savefig(
                os.path.join(folder_name, "Logo - Cluster {}.png".format(ind)),
                bbox_inches="tight",
                dpi=pyp.DEFAULT_DPI,
                transparent=True,
            )

    slices = [
        data["ds"].filter({"series": y_pred == ind})
        for ind in ss
    ]

    for ind, s in zip(ss, slices):
        s.name = "Cluster {}".format(ind)

    pyp.tables.write_full_tables(
        slices,
        folder_name=folder_name,
    )

    if filename:
        with open(os.path.join(folder_name, filename), "wb") as f:
            pickle.dump((data, y_pred), f)

    return data, y_pred
