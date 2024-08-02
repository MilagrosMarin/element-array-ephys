from __future__ import annotations

import os
import datajoint as dj
from datetime import datetime, timezone
from pathlib import Path
import json
import shutil

from element_interface.utils import dict_to_uuid, find_full_path

logger = dj.logger

schema = dj.schema()

ephys = None
ephys_sorter = None


def activate(
    schema_name,
    *,
    ephys_module,
    ephys_sorter_module,
    create_schema=True,
    create_tables=True,
):
    """
    activate(schema_name, *, ephys_module, ephys_sorter_module, create_schema=True, create_tables=True)
        :param schema_name: schema name on the database server to activate the `ephys_curation` schema
        :param ephys_module: the activated ephys module for which this `ephys_curation` schema will be downstream from
        :param ephys_sorter_module: the activated ephys_sorter module for which this `ephys_curation` schema will be downstream from
        :param create_schema: when True (default), create schema in the database if it does not yet exist.
        :param create_tables: when True (default), create tables in the database if they do not yet exist.
    """
    global ephys, ephys_sorter
    ephys = ephys_module
    ephys_sorter = ephys_sorter_module
    schema.activate(
        schema_name,
        create_schema=create_schema,
        create_tables=create_tables,
        add_objects={**ephys.__dict__, **ephys_sorter.__dict__},
    )


@schema
class CurationMethod(dj.Lookup):
    definition = """
    # Curation method
    curation_method: varchar(16)  # method/package used to perform manual curation (e.g. Phy, FigURL, etc.)
    """
    contents = [
        ("Phy",),
    ]


@schema
class ManualCuration(dj.Manual):
    definition = """
    # Manual curation from an ephys.Clustering
    -> ephys.Clustering
    curation_id: int
    ---
    curation_datetime: datetime    # UTC time when the curation was performed
    parent_curation_id=-1: int     # if -1, this curation is based on the raw spike sorting results
    -> CurationMethod              # which method/package used for manual curation (inform how to ingest the results)
    description="": varchar(1000)  # user-defined description/note of the curation
    """

    class File(dj.Part):
        definition = """
        -> master
        file_name: varchar(255)
        ---
        file: filepath@ephys-processed
        """

    @classmethod
    def prepare_manual_curation(
        cls,
        key,
        *,
        parent_curation_id=-1,
        curation_method="Phy",
        if_exists="skip",
        download_binary=False,
    ):
        """
        Create a new directory to for a new round of manual curation
        Download the spike sorting results for new manual curation from the specified "key" and "parent_curation_id".
        Store the initial meta information in json file.
        Args:
            key (dict): PK of the ephys.Clustering table
            parent_curation_id (int): curation_id to be used as the starting point for this new curation
            curation_method (str): method/package used for manual curation (e.g. Phy)
            if_exists (str): overwrite|skip
            download_binary (bool): if True, also download the raw ephys (.dat) file

        Returns:
            directory where the spike sorting results are downloaded
        """
        assert if_exists in [
            "overwrite",
            "skip",
        ], f"Invalid if_exists: {if_exists}"

        if curation_method != "Phy":
            raise ValueError(f"Unsupported curation method: {curation_method}")

        init_datetime = datetime.now(timezone.utc)

        # Download the spike sorting results
        assert ephys.Clustering & key, f"Invalid ephys.Clustering key: {key}"

        if parent_curation_id == -1:
            assert (
                ephys_sorter.SIExport & key
            ), "SIExport not found for the specified key"
            files_query = (
                ephys_sorter.SIExport.File
                & key
                & "file_name LIKE 'phy%' AND file_name NOT LIKE '%recording.dat'"
            )
        else:
            assert cls & {
                **key,
                "curation_id": parent_curation_id,
            }, "ManualCuration not found for the specified key"
            files_query = cls.File & {**key, "curation_id": parent_curation_id}

        # create new directory for new curation
        output_dir = (ephys.ClusteringTask & key).fetch1("clustering_output_dir")
        output_dir = (
            Path(ephys.get_processed_root_data_dir()) / output_dir / "curations"
        )

        dirname = f"{curation_method}_parentID_" + (
            "orig" if parent_curation_id == -1 else f"{parent_curation_id}"
        )
        curation_output_dir = output_dir / dirname
        curation_output_dir.mkdir(parents=True, exist_ok=True)

        # download spike sorting files and copy to the new directory
        logger.info(
            f"New manual curation: {curation_output_dir} - Downloading {len(files_query)} files..."
        )

        for f in files_query.fetch("file"):
            f = Path(f)
            if f.name.startswith(".") and f.suffix in (".json", ".pickle"):
                continue
            new_f = curation_output_dir / f.name
            if not new_f.exists() or if_exists == "overwrite":
                shutil.copy2(f, new_f)

        if download_binary:
            new_f = curation_output_dir / "recording.dat"
            if not new_f.exists() or if_exists == "overwrite":
                raw_file_query = (
                    ephys_sorter.SIExport.File & key & "file_name = 'phy/recording.dat'"
                )
                if raw_file_query:
                    logger.info("Downloading raw ephys data file...")
                    f = Path(raw_file_query.fetch1("file"))
                    shutil.copy2(f, new_f)
                else:
                    logger.warning("Raw ephys data file not found.")

        # write "entry" into a json file
        with open(curation_output_dir / ".manual_curation_entry.json", "w") as f:
            json.dump(
                {
                    **key,
                    "parent_curation_id": parent_curation_id,
                    "curation_method": curation_method,
                },
                f,
                default=str,
            )

        return curation_output_dir

    @classmethod
    def insert_manual_curation(
        cls,
        curation_output_dir,
        *,
        key=None,
        parent_curation_id=None,
        curation_method="Phy",
        description="",
        delete_local_dir=True,
    ):
        """
        Insert a new manual curation into the database
        1. Get a new "curation_id" (auto-incremented)
        2. Copy the curation_output_dir to a new directory with the new curation_id
        3. Insert the new curation into the database (excluding the raw recording.dat file)
        4. Delete the old curation directory
        5. Optionally delete the new curation directory
        Args:
            curation_output_dir: directory where the curation results are stored
            key: ephys.Clustering key
            parent_curation_id: curation_id of the parent curation
            curation_method: method/package used for manual curation (e.g. Phy)
            description: user-defined description/note of the curation
            delete_local_dir: if True, delete the new curation directory after inserting the curation into the database

        Returns: `key` of the newly inserted manual curation
        """

        curation_output_dir = Path(curation_output_dir)

        # Light logic to safeguard against re-inserting the same curation
        if (curation_output_dir / ".manual_curation_entry.json").exists():
            if key is not None:
                logger.warning(".manual_curation_entry.json already exists. Ignoring inputs.")

            with open(curation_output_dir / ".manual_curation_entry.json", "r") as f:
                key = json.load(f)

            parent_curation_id = key.pop("parent_curation_id")
            curation_method = key.pop("curation_method")

            if "curation_id" in key:
                print(f"Manual curation already inserted: {key}")
                if (
                    dj.utils.user_choice("Insert a new manual curation anyway?")
                    != "yes"
                ):
                    print("Canceled")
                    return
        else:
            if parent_curation_id is None or key is None:
                raise ValueError(".manual_curation_entry.json not found. `key` AND `parent_curation_id` must be specified")

        if curation_method != "Phy":
            raise ValueError(f"Unsupported curation method: {curation_method}")

        from element_array_ephys.readers import kilosort

        kilosort_dataset = kilosort.Kilosort(curation_output_dir)
        assert (
            kilosort_dataset.data
        ), f"Invalid Phy output directory: {curation_output_dir}"

        curate_datetime = datetime.now(timezone.utc)
        curation_id = (
            ephys.Clustering.aggr(cls, count="count(curation_id)", keep_all_rows=True)
            & key
        ).fetch1("count") + 1
        logger.info(f"New curation id: {curation_id}")

        key["curation_id"] = curation_id
        with cls.connection.transaction:
            entry = {
                **key,
                "curation_datetime": curate_datetime,
                "parent_curation_id": parent_curation_id,
                "curation_method": curation_method,
                "description": description,
            }
            cls.insert1(entry)

            # rename curation_output_dir folder into curation_id (skip the raw recording.dat file)
            new_curation_output_dir = (
                curation_output_dir.parent
                / f"{curation_method}_curationID_{curation_id}"
            )
            new_curation_output_dir.mkdir(parents=True, exist_ok=True)
            for f in curation_output_dir.glob("*"):
                if f.is_file() and f.name != "recording.dat":
                    shutil.copy2(
                        f, new_curation_output_dir / f.relative_to(curation_output_dir)
                    )
                elif f.is_dir():
                    shutil.copytree(
                        f, new_curation_output_dir / f.relative_to(curation_output_dir)
                    )

            logger.info(f"Inserting files from {new_curation_output_dir}...")
            cls.File.insert(
                [
                    {
                        **key,
                        "file_name": f.relative_to(new_curation_output_dir).as_posix(),
                        "file": f,
                    }
                    for f in new_curation_output_dir.rglob("*")
                    if f.is_file() and f.name not in ("recording.dat", ".manual_curation_entry.json")
                ]
            )

        logger.info(f"New manual curation successfully inserted: {key}")

        # write "entry" into a json file
        with open(new_curation_output_dir / ".manual_curation_entry.json", "w") as f:
            json.dump(entry, f, default=str)

        # Delete the old curation directory
        try:
            shutil.rmtree(curation_output_dir)
        except Exception as e:
            logger.error(f"Failed to fully delete the old curation directory (please try to delete manually):\n\t{curation_output_dir}\n{e}")

        if delete_local_dir:
            try:
                shutil.rmtree(new_curation_output_dir)
            except Exception as e:
                logger.error(f"Failed to fully delete the new curation directory (please try to delete manually):\n\t{new_curation_output_dir}\n{e}")

        return key


def launch_phy(key, parent_curation_id=-1, download_binary=False):
    """
    Select a spike sorting key for manual curation
    1. download the spike sorting results
    2. launch phy
    3. commit new curation locally
    4. insert new curation into the database
    Args:
        key: ephys.Clustering key
        parent_curation_id: if -1, this curation is based on the raw spike sorting results
        download_raw: if True, also download the raw ephys (.dat) file
        do_insert: if True, insert the new curation into the database (upload result files)
    """
    from phy.apps.template import template_gui

    curation_output_dir = ManualCuration.prepare_manual_curation(
        key, parent_curation_id=parent_curation_id, download_binary=download_binary
    )

    template_gui(curation_output_dir / "params.py")

    description = input("Curation description: ")

    ManualCuration.insert_manual_curation(
        curation_output_dir,
        description=description,
    )
