import json
import os
import zipfile
import shutil
import glob
import re
import configparser

# import something to do a get on the github api
from numpy import mat
import requests
import urllib.request

ASSET_LIB_ENDPOINT = "https://godotengine.org/asset-library/api"
ASSET_LIB_ASSET_ENDPOINT = "https://godotengine.org/asset-library/api/asset?type=addon&max_results=100&filter={}"
GITHUB_RELEASES_ENDPOINT = "https://api.github.com/repos/GodotSteam/GodotSteam/releases?per_page=100&page={}"
GODOT_VERSION_RELEASE_DATES = {
    "2.0": "2016-02-23",
    "2.1": "2016-09-08",
    "3.0": "2018-01-29",
    "3.1": "2019-03-13",
    "3.2": "2020-01-29",
    "3.3": "2021-04-22",
    "3.4": "2021-11-06",
    "3.5": "2022-08-05",
    "3.6": "2024-09-09",
    "4.0": "2023-03-01",
    "4.1": "2023-07-06",
    "4.2": "2023-11-30",
    "4.3": "2024-08-15",
}

DO_DOWNLOAD = True


THIS_DIR = os.path.dirname(os.path.realpath(__file__))
TMP_DIR = THIS_DIR + "/.tmp"
TEMPLATE_PATH = THIS_DIR + "/misc/plugin_versions.h.inc"
HEADER_PATH = THIS_DIR + "/utility/plugin_versions.h"

TEMPLATE = """#pragma once
// This file is autogenerated by godot_steam_versions.py
// clang-format off
#include <core/templates/vector.h>
struct PluginBin {
    const char* name; 
    const char* md5; 
    const char* platform;
};

struct PluginVersion {
    const char* name;
    const char* min_godot_version;
    const char* max_godot_version;
    const char* version;
    const char* url;
    const char* platforms;
    Vector<PluginBin> bins;
};

"""

plugin_vector_template = """
static const Vector<PluginVersion> {0}_plugin_versions = {{
{1}
}};
"""

template_suffix = """
const Vector<PluginVersion> get_plugin_versions(const String& name) {{
{0}
}}
"""

plugin_getter_template = """
    if (name == "{0}") {{
        return {0}_plugin_versions;
    }}
"""


def get_assetlib_releases(url: str):
    releases = []
    page = 1
    while True:
        response = requests.get(url.format(page))
        if response.status_code != 200:
            break
        page_releases = json.loads(response.text)
        if not page_releases:
            break
        releases.extend(page_releases)
        page += 1
    # name to object
    # releases_dict: dict[str, dict] = {}
    # for release in releases:
    #     releases_dict[release["name"]] = release
    return releases


import hashlib
from pathlib import Path


def md5_update_from_file(filename, hash):
    assert Path(filename).is_file()
    with open(str(filename), "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash.update(chunk)
    return hash


def md5_file(filename):
    return md5_update_from_file(filename, hashlib.md5()).hexdigest()


def md5_update_from_dir(directory, hash):
    assert Path(directory).is_dir()
    paths = glob.glob(str(directory) + "/**/*", recursive=True)
    paths = sorted(paths, key=lambda p: str(p))
    for path in paths:
        if not Path(path).is_file() or "_CodeSignature" in path:
            continue
        hash = md5_update_from_file(path, hash)
    return hash


def md5_dir(directory):
    return md5_update_from_dir(directory, hashlib.md5()).hexdigest()


def get_version_from_url_and_release_asset(url: str, release_name: str, asset) -> tuple[str, str]:
    parts = release_name.split("-")
    first_part = parts[0].replace("Godot", "").strip()
    asset: dict

    def strip_x(version: str) -> str:
        if version.endswith(".x"):
            if version.count(".") > 1:
                return version[:-2]
        return version

    max_godot_version = first_part.split("/")[-1].strip()
    if max_godot_version == "3.x":
        max_godot_version = "3.6"
        min_godot_version = "3.5"
    else:
        max_godot_version = strip_x(max_godot_version)
        if "/" in first_part:
            min_godot_version = strip_x(first_part.split("/")[0].strip())
        else:
            min_godot_version = max_godot_version
    plugin_version = parts[2].replace("GDNative", "").replace("GDExtension", "").strip().split(" ")[-1].strip()

    filename: str = url.split("/")[-1]
    # get the basename of the file (minus the extension)
    basename = filename.rsplit(".", 1)[0]
    re_str = r"(?:(\d\.\d\.\d)-addons)|(?:[^\.](\d{2,3}))(?:-(\d{1,2}))?$"
    match = re.search(re_str, basename)
    if match:
        patch_number = 0
        if match.group(2) and len(match.group(2)) >= 2:
            min_godot_version = match.group(2)[0] + "." + match.group(2)[1] + ".0"
            if len(match.group(2)) > 2:
                patch_number = int(match.group(2)[2:])
        else:
            min_godot_version = match.group(1)
            # remove the patch number and put on .0
            if len(min_godot_version) > 3:
                orts = min_godot_version.rsplit(".", 1)
                min_godot_version = orts[0] + ".0"
                if len(orts) > 1:
                    patch_number = int(orts[1])
        if match.group(3):
            max_godot_version = match.group(3)[0] + "." + match.group(3)[1]
        else:
            max_godot_version = min_godot_version.rsplit(".", 1)[0]
        max_godot_version += f".{patch_number}"
    return min_godot_version, max_godot_version, plugin_version


def parse_gdnative_gdextension_releases(url: str, plugin_name: str, get_vers: callable):
    releases: list[dict] = get_assetlib_releases(url)
    # dump releases to tmp dir
    ext_releases = []
    for release in releases:
        name: str = release["name"].strip()
        ext_releases.append(release)
    # ensure the dir
    WORKING_DIR = TMP_DIR + "/" + plugin_name
    os.makedirs(WORKING_DIR, exist_ok=True)

    with open(WORKING_DIR + f"/{plugin_name}_releases.json", "w") as f:
        json.dump(ext_releases, f, indent=2)
    version_dict: dict[str, dict] = {}
    versions: set[str] = set()
    # ensure dir
    os.makedirs(WORKING_DIR, exist_ok=True)
    # reverse sort the releases
    releases = sorted(releases, key=lambda x: x["created_at"], reverse=False)

    for release in releases:
        name: str = release["name"].strip()
        asset: dict
        for asset in release["assets"]:
            if not (".zip" in url.lower() or ".7z" in url.lower()):
                continue
            url: str = asset["browser_download_url"]
            filename: str = url.split("/")[-1]
            min_godot_version, max_godot_version, plugin_version = get_vers(asset["browser_download_url"], name, asset)

            # browser_download_url
            if name in version_dict:
                name += "_1"

            zip_path = WORKING_DIR + "/" + filename
            unzipped_folder = WORKING_DIR + "/" + name.replace(" ", "_").replace("/", "_")  # filename.rsplit(".", 1)[0]

            new_path, msg = urllib.request.urlretrieve(url, zip_path)
            # unzip the file to a folder with the same name as the release
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(unzipped_folder)

            # go to the unzipped_folder/addons/godotsteam and get the first-level folders
            addon_folder = unzipped_folder + f"/addons/{plugin_name}"
            if not os.path.exists(addon_folder):
                print("Addon folder not found in", addon_folder)
                continue
            # get the first-level folders
            addon_folders = os.listdir(addon_folder)
            platform_folders = []
            plugin_bins: list[dict] = []
            for platform_name in addon_folders:
                addon_folder_path = addon_folder + "/" + platform_name
                if not os.path.isdir(addon_folder_path):
                    continue
                platform_folders.append(platform_name)
            for platform_name in platform_folders:
                addon_folder_path = addon_folder + "/" + platform_name
                if not os.path.isdir(addon_folder_path):
                    continue
                # list the files in the folder
                files = os.listdir(addon_folder_path)
                for file in files:
                    # get an md5sum of the file/folder
                    file_path = addon_folder_path + "/" + file
                    if os.path.isdir(file_path):
                        md5 = md5_dir(file_path)
                    else:
                        md5 = md5_file(file_path)
                    file_dict = {"name": file, "md5": md5, "platform": platform_name}
                    plugin_bins.append(file_dict)
            version_dict[name] = {
                "platforms": platform_folders,
                "url": url,
                "min_godot_version": min_godot_version,
                "max_godot_version": max_godot_version,
                "version": plugin_version,
                "bins": plugin_bins,
            }
            try:
                pass
                # shutil.rmtree(unzipped_folder)
                os.remove(zip_path)
            except Exception as e:
                print("Error removing files", e)
    return version_dict


def write_plugin_versions(url, name, get_vers):
    version_dict = parse_gdnative_gdextension_releases(url, name, get_vers)
    JSON_PATH = THIS_DIR + f"/misc/{name}_plugin_versions.json"

    # dump it to a file
    try:
        with open(JSON_PATH, "w") as f:
            json.dump(version_dict, f, indent=2)
    except Exception as e:
        # just print it out
        print(version_dict)


def write_header_file():
    # open the previously created file
    version_dict = {}
    JSON_PATH = THIS_DIR + f"/misc/{name}_plugin_versions.json"
    with open(JSON_PATH, "r") as f:
        version_dict = json.load(f)

    if not version_dict:
        print("No version dict found!!!!")
        return
    # read in the TEMPLATE_PATH as string and replace the version data
    with open(TEMPLATE_PATH, "r") as f:
        header = f.read()
    if not header:
        print("No template found!!!!")
        return
    REPLACE_PART = "// _GODOTSTEAM_VERSIONS_BODY_"
    version_data = ""
    for name, data in version_dict.items():
        INDENT = "\n\t\t\t"
        bins = INDENT + f",{INDENT}".join(
            f'{{"{bin["name"]}", "{bin["md5"]}", "{bin["platform"]}"}}' for bin in data["bins"]
        )
        version_def = f'\t{{"{name}", "{data["min_godot_version"]}", "{data["max_godot_version"]}", "{data["version"]}", "{data["url"]}", "{", ".join(data["platforms"])}",\n\t\t{{{bins}}}}}'
        version_data += version_def + ",\n"

    data = header.replace(REPLACE_PART, version_data)
    with open(HEADER_PATH, "w") as f:
        f.write(data)


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


# def trawl_asset_lib():
#     URL = "https://godotengine.org/asset-library/api/asset/{0}"
#     missing_in_a_row = 0
#     MAX_ASSET_ID = 4000
#     MAX_MISSING_IN_A_ROW = 30
#     assets = []
#     for asset_id in range(1, MAX_ASSET_ID):
#         response = requests.get(URL.format(asset_id))
#         if response.status_code == 404:
#             missing_in_a_row += 1
#             if missing_in_a_row > MAX_MISSING_IN_A_ROW:
#                 break
#             continue
#         missing_in_a_row = 0
#         asset = json.loads(response.text)
#         assets.append(asset)
#     ensure_dir(TMP_DIR)
#     with open(TMP_DIR + "/assets.json", "w") as f:
#         json.dump(assets, f, indent=2)


# GODOT_VERSIONS = ["2.0", "2.1", "3.0", "3.1", "3.2", "3.3", "3.4", "3.5", "3.6", "4.0", "4.1", "4.2", "4.3"]
GODOT_VERSIONS = ["2.99", "3.99", "4.99"]


def trawl_asset_lib():
    URL = "https://godotengine.org/asset-library/api/asset?type=addon&godot_version={0}&max_results={1}&page={2}"
    MAX_RESULTS = 500
    assets = []
    for godot_version in GODOT_VERSIONS:
        page = 0
        while True:
            request_url = URL.format(godot_version, MAX_RESULTS, page)
            response = requests.get(request_url)
            if response.status_code != 200:
                break
            response = json.loads(response.text)
            pages = response["pages"]
            page_assets = response["result"]
            if not page_assets:
                break
            assets.extend(page_assets)
            if page >= pages:
                break
            page += 1
    # sort by asset_id
    assets = sorted(assets, key=lambda x: int(x["asset_id"]))
    ensure_dir(TMP_DIR)
    with open(TMP_DIR + f"/assets.json", "w") as f:
        json.dump(assets, f, indent=2)


def trawl_asset_lib_for_plugin(plugin_name: str):
    URL = "https://godotengine.org/asset-library/api/asset?type=addon&filter={0}&godot_version={1}&max_results={2}&page={3}"
    MAX_RESULTS = 500
    assets = []
    for godot_version in GODOT_VERSIONS:
        page = 0
        pages = 1000
        while page < pages:
            request_url = URL.format(plugin_name, godot_version, MAX_RESULTS, page)
            response = requests.get(request_url)
            if response.status_code != 200:
                break
            response = json.loads(response.text)
            pages = response["pages"]
            page_assets = response["result"]
            if not page_assets:
                continue
            assets.extend(page_assets)
            page += 1
    # sort by asset_id
    assets = sorted(assets, key=lambda x: int(x["asset_id"]))
    asset_ids: set[int] = {int(asset["asset_id"]) for asset in assets}
    assets = []
    NEW_URL = "https://godotengine.org/asset-library/api/asset/{0}"
    for asset_id in asset_ids:
        response = requests.get(NEW_URL.format(asset_id))
        if response.status_code == 200:
            asset = json.loads(response.text)
            assets.append(asset)
    return assets
    ensure_dir(TMP_DIR)
    with open(TMP_DIR + f"/{plugin_name}_assets.json", "w") as f:
        json.dump(found, f, indent=2)


def get_list_of_edits(asset_id: int):
    URL = "https://godotengine.org/asset-library/api/asset/edit?asset={0}&status=accepted"
    response = requests.get(URL.format(asset_id))
    if response.status_code != 200:
        return []
    response_obj = json.loads(response.text)
    edits = response_obj["result"]
    # sort them by edit id
    edits = sorted(edits, key=lambda x: int(x["edit_id"]))
    return edits


# https://godotengine.org/asset-library/api/asset/{ASSET_ID}
# asset return dict is like this:
# ```json
# {
#   "asset_id": "1",
#   "type": "addon",
#   "title": "Snake",
#   "author": "test",
#   "author_id": "1",
#   "version": "1",
#   "version_string": "alpha",
#   "category": "2D Tools",
#   "category_id": "1",
#   "godot_version": "2.1",
#   "rating": "0",
#   "cost": "GPLv3",
#   "description": "Lorem ipsum…",
#   "support_level": "testing",
#   "download_provider": "GitHub",
#   "download_commit": "master",
#   "download_hash": "(sha256 hash of the downloaded zip)",
#   "browse_url": "https://github.com/…",
#   "issues_url": "https://github.com/…/issues",
#   "icon_url": "https://….png",
#   "searchable": "1",
#   "modify_date": "2018-08-21 15:49:00",
#   "download_url": "https://github.com/…/archive/master.zip",
#   "previews": [
#     {
#       "preview_id": "1",
#       "type": "video",
#       "link": "https://www.youtube.com/watch?v=…",
#       "thumbnail": "https://img.youtube.com/vi/…/default.jpg"
#     },
#     {
#       "preview_id": "2",
#       "type": "image",
#       "link": "https://….png",
#       "thumbnail": "https://….png"
#     }
#   ]
# }
# ```

# edit list api: https://godotengine.org/asset-library/api/asset/edit?asset={ASSET_ID}&status=accepted
# edit list is like this:
# ```json
# [
#         {
#             "edit_id": "14471",
#             "asset_id": "1918",
#             "user_id": "8098",
#             "submit_date": "2024-11-03 17:51:55",
#             "modify_date": "2024-11-04 10:58:07",
#             "title": "Godot Jolt",
#             "description": "Godot Jolt is a native extension that allows you to use the Jolt physics engine to power Godot's 3D physics.\r\n\r\nIt functions as a drop-in replacement for Godot Physics, by implementing the same nodes that you would use normally, like RigidBody3D or CharacterBody3D.\r\n\r\nThis version of Godot Jolt only supports Godot 4.3 (including 4.3.x) and only support Windows, Linux, macOS, iOS and Android.\r\n\r\nOnce the extension is extracted in your project folder, you need to go through the following steps to switch physics engine:\r\n\r\n1. Restart Godot\r\n2. Open your project settings\r\n3. Make sure \"Advanced Settings\" is enabled\r\n4. Go to \"Physics\" and then \"3D\"\r\n5. Change \"Physics Engine\" to \"JoltPhysics3D\"\r\n6. Restart Godot\r\n\r\nFor more details visit: github.com\/godot-jolt\/godot-jolt\r\nFor more details about Jolt itself visit: github.com\/jrouwe\/JoltPhysics",
#             "godot_version": "4.3",
#             "version_string": "0.14.0",
#             "cost": "MIT",
#             "browse_url": "https:\/\/github.com\/godot-jolt\/godot-jolt",
#             "icon_url": "https:\/\/github.com\/godot-jolt\/godot-asset-library\/releases\/download\/v0.14.0-stable\/godot-jolt_icon.png",
#             "category": null,
#             "support_level": "community",
#             "status": "accepted",
#             "reason": "",
#             "author": "mihe"
#         },
#         ...
# ]
# ```json

# edit api: https://godotengine.org/asset-library/api/asset/edit/{EDIT_ID}
# edit is like this:
# ```json
# {
#     "edit_id": "8057",
#     "asset_id": "1918",
#     "user_id": "8098",
#     "title": "Godot Jolt",
#     "description": "Godot Jolt is a native extension that allows you to use the Jolt physics engine to power Godot's 3D physics.\r\n\r\nIt functions as a drop-in replacement for Godot Physics, by implementing the same nodes that you would use normally, like RigidBody3D or CharacterBody3D.\r\n\r\nThis version of Godot Jolt supports Godot 4.0.3.\r\n\r\nPlease note that C# for Godot doesn't officially support GDExtension yet, and as such you may get errors when using certain physics features from C#.\r\n\r\nOnce the extension is extracted in your project folder, you need to perform the following steps to actually switch physics engine:\r\n\r\n1. Start (or restart) Godot\r\n2. Open your project settings\r\n3. Make sure \"Advanced Settings\" is enabled\r\n4. Go to \"Physics\" and then \"3D\"\r\n5. Change \"Physics Engine\" to \"JoltPhysics3D\"\r\n6. Restart Godot\r\n\r\nFor more details, release notes, discussions and more, please visit the project's GitHub page at: https:\/\/github.com\/godot-jolt\/godot-jolt",
#     "category_id": "7",
#     "godot_version": "4.0",
#     "version_string": "0.2.1",
#     "cost": "MIT",
#     "download_provider": "Custom",
#     "download_commit": "https:\/\/github.com\/godot-jolt\/godot-jolt\/releases\/download\/v0.2.1-stable\/godot-jolt_v0.2.1-stable.zip",
#     "browse_url": "https:\/\/github.com\/godot-jolt\/godot-jolt",
#     "issues_url": "https:\/\/github.com\/godot-jolt\/godot-jolt\/issues",
#     "icon_url": "https:\/\/raw.githubusercontent.com\/godot-jolt\/godot-jolt\/master\/docs\/icon.png",
#     "status": "accepted",
#     "reason": "",
#     "type": null,
#     "link": null,
#     "thumbnail": null,
#     "operation": null,
#     "unedited_preview_id": null,
#     "unedited_type": null,
#     "unedited_link": null,
#     "unedited_thumbnail": null,
#     "author": "mihe",
#     "previews": [],
#     "original": {
#         "asset_id": "1918",
#         "user_id": "8098",
#         "title": "Godot Jolt",
#         "description": "Godot Jolt is a native extension that allows you to use the Jolt physics engine to power Godot's 3D physics.\r\n\r\nIt functions as a drop-in replacement for Godot Physics, by implementing the same nodes that you would use normally, like RigidBody3D or CharacterBody3D.\r\n\r\nThis version of Godot Jolt only supports Godot 4.3 (including 4.3.x) and only support Windows, Linux, macOS, iOS and Android.\r\n\r\nOnce the extension is extracted in your project folder, you need to go through the following steps to switch physics engine:\r\n\r\n1. Restart Godot\r\n2. Open your project settings\r\n3. Make sure \"Advanced Settings\" is enabled\r\n4. Go to \"Physics\" and then \"3D\"\r\n5. Change \"Physics Engine\" to \"JoltPhysics3D\"\r\n6. Restart Godot\r\n\r\nFor more details visit: github.com\/godot-jolt\/godot-jolt\r\nFor more details about Jolt itself visit: github.com\/jrouwe\/JoltPhysics",
#         "category_id": "7",
#         "godot_version": "4.3",
#         "version": "18",
#         "version_string": "0.14.0",
#         "cost": "MIT",
#         "rating": "0",
#         "support_level": "1",
#         "download_provider": "Custom",
#         "download_commit": "https:\/\/github.com\/godot-jolt\/godot-asset-library\/releases\/download\/v0.14.0-stable\/godot-jolt_v0.14.0-stable.zip",
#         "browse_url": "https:\/\/github.com\/godot-jolt\/godot-jolt",
#         "issues_url": "https:\/\/github.com\/godot-jolt\/godot-jolt\/issues",
#         "icon_url": "https:\/\/github.com\/godot-jolt\/godot-asset-library\/releases\/download\/v0.14.0-stable\/godot-jolt_icon.png",
#         "searchable": "1",
#         "modify_date": "2024-11-04 10:58:07",
#         "download_url": "https:\/\/github.com\/godot-jolt\/godot-asset-library\/releases\/download\/v0.14.0-stable\/godot-jolt_v0.14.0-stable.zip"
#     },
#     "download_url": "https:\/\/github.com\/godot-jolt\/godot-jolt\/releases\/download\/v0.2.1-stable\/godot-jolt_v0.2.1-stable.zip"
# }
# ```
# Top level fields will only be present if they were changed in the edit.


def download_and_unzip(download_url: str, plugin_name: str, version: str):
    WORKING_DIR = TMP_DIR + "/" + plugin_name
    os.makedirs(WORKING_DIR, exist_ok=True)
    zip_path: str = WORKING_DIR + "/" + download_url.split("/")[-1]
    unzipped_folder: str = WORKING_DIR + "/" + version
    if DO_DOWNLOAD:
        new_path, msg = urllib.request.urlretrieve(download_url, zip_path)
        # unzip the file to a folder with the same name as the release
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(unzipped_folder)
    return unzipped_folder


# def get_plugin_versions(plugin_name: str):
def get_plugin_versions(plugin_name: str, asset_id: int):
    edits = get_list_of_edits(asset_id)
    versions = []
    for edit_list_entry in edits:
        edit_id = edit_list_entry["edit_id"]
        edit_url = f"https://godotengine.org/asset-library/api/asset/edit/{edit_id}"
        response = requests.get(f"https://godotengine.org/asset-library/api/asset/edit/{edit_id}")
        if response.status_code != 200:
            continue
        edit = json.loads(response.text)
        godot_version = edit_list_entry["godot_version"]
        submit_date = edit_list_entry["submit_date"]
        if submit_date:
            submit_date = submit_date.split(" ")[0]
        version = edit["version_string"]
        download_commit = edit["download_commit"]
        if not version or not download_commit:
            continue
        # check if download_url begins with "http"
        if not download_commit.startswith("http"):
            download_url = edit["download_url"]
            if not download_url or not download_url.startswith("http"):
                print(f"Invalid download url: {download_url}")
                continue
            download_commit = download_url
        if godot_version in edit and edit["godot_version"]:
            godot_version = edit["godot_version"]
        # check the date of the godot version
        release_date = GODOT_VERSION_RELEASE_DATES.get(godot_version)
        current_version = godot_version
        if not release_date:
            print(f"Godot version {godot_version} not found in release dates")
        else:
            if submit_date < release_date:
                # look for the lowest godot version that is higher than the release date
                for engine_version, date in [
                    (engine_version, date)
                    for engine_version, date in GODOT_VERSION_RELEASE_DATES.items()
                    if current_version[0] == engine_version[0]
                ]:
                    if date <= submit_date and current_version >= engine_version:
                        godot_version = engine_version

        print(
            f"plugin {plugin_name} version: {version}, Godot version: {godot_version}, download_url: {download_commit}"
        )
        version_dict = {
            "version": version,
            "min_godot_version": godot_version,
            "max_godot_version": godot_version,
            "submit_date": submit_date,
            "download_url": download_commit,
        }
        versions.append(version_dict)
    return versions


def get_plugin_info(plugin_name: str):
    assets = trawl_asset_lib_for_plugin(plugin_name)
    if (len(assets)) == 0:
        print(f"No assets found for {plugin_name}")
        return
    valid_assets = []
    if (len(assets)) > 1:
        # we have to download and unzip them, then find the addon folder
        for ast in assets:
            download_url = ast["download_url"]
            unzipped_folder = download_and_unzip(download_url, plugin_name, ast["version_string"])
            addon_prefix = get_addon_name(unzipped_folder)
            if addon_prefix == plugin_name:
                valid_assets.append(ast)
    else:
        valid_assets = [assets[0]]
    if not valid_assets:
        print("No matching asset found for " + plugin_name)
        return

    new_versions = []
    versions = []
    for asset in valid_assets:
        asset_id = asset["asset_id"]
        new_versions.extend(get_plugin_versions(plugin_name, int(asset_id)))
    for version in new_versions:
        hash = get_version_hashes(plugin_name, version)
        if not hash:
            continue
        versions.append(hash)
    plugin_info = {
        "name": plugin_name,
        "asset_id": asset_id,
        "versions": versions,
    }
    WORKING_DIR = TMP_DIR + "/" + plugin_name
    os.makedirs(WORKING_DIR, exist_ok=True)
    with open(WORKING_DIR + f"/{plugin_name}_info.json", "w") as f:
        json.dump(plugin_info, f, indent=2)


def get_gdext_bin_info(entry_name: str, lib: str, addon_folder: str):
    lib = lib.strip('"')
    parts = entry_name.split(".")
    plat = parts[0]
    release_type = "release" if "release" in entry_name else ("debug" if "debug" in entry_name else "")
    arch = parts[-1] if "64" in entry_name or "32" in entry_name else ""
    path = addon_folder + "/" + lib
    bin = {"name": lib, "platform": plat, "release": release_type, "arch": arch, "md5": ""}
    # check if the path is a file or a directory
    if not (os.path.exists(path)):
        parts = lib.split(".", 1)
        if len(parts) > 1:
            lib = f"{parts[0]}.{plat}.{parts[1]}"
            path = addon_folder + "/" + lib
            if not (os.path.exists(path)):
                print(f"File {path} not found")
                return bin
            bin["name"] = lib
        else:
            print(f"File {path} not found")
            return bin

    if os.path.isdir(path):
        bin["md5"] = md5_dir(path)
    else:
        bin["md5"] = md5_file(path)
    return bin


def get_addon_dir(unzipped_folder):
    paths = glob.glob(unzipped_folder + f"/**/addons", recursive=True)
    if not paths:
        print("Addon folder not found in", unzipped_folder)
        return ""
    # do a dir listing on paths[0] (addons), it'll be the first one
    for dir in os.listdir(paths[0]):
        fold = paths[0] + "/" + dir
        if Path(fold).is_dir():
            return fold
    print("Addon folder not found in", paths[0])
    return ""


def get_addon_name(unzipped_folder):
    addon_folder = get_addon_dir(unzipped_folder)
    if not addon_folder:
        return ""
    # just get the last part of the path
    return addon_folder.split("/")[-1]


def get_version_hashes(plugin_name, version_info: dict):
    version = version_info["version"]
    download_url = version_info["download_url"]
    filename = download_url.split("/")[-1]
    unzipped_folder = download_and_unzip(download_url, plugin_name, version)
    addon_prefix = f"addons/{plugin_name}"

    addon_folder = unzipped_folder + f"/{addon_prefix}"
    if not os.path.exists(addon_folder):
        # do a search in the unzipped_folder for it
        addon_folder = get_addon_dir(unzipped_folder)
        if not addon_folder:
            return None
        addon_prefix = f"addons/{addon_folder.split("/")[-1]}"

    REPLACE_ABS = f"res://{addon_prefix}/"
    REPLACE_ABS2 = f"res://addon/{plugin_name}/"

    def sanitize_lib(lib: str):
        return lib.strip('"').removeprefix(REPLACE_ABS).removeprefix(REPLACE_ABS2)

    # find the .gdextension or .gdnlib file in the addon folder

    # look for *.gdextension or *.gdnlib file
    files = os.listdir(addon_folder)
    gdextension_files = [file for file in files if file.endswith(".gdextension") or file.endswith(".gdnlib")]
    if not gdextension_files:
        print(f"No .gdextension or .gdnlib files found in {addon_folder}")
        return None
    if (len(gdextension_files)) > 1:
        print(f"Multiple .gdextension or .gdnlib files found in {addon_folder}")
    gdextension_file = gdextension_files[0]
    is_gdnative = gdextension_file.endswith(".gdnlib")

    # it's an inifile
    ini_file = configparser.ConfigParser()
    ini_file.read(addon_folder + "/" + gdextension_file)

    if is_gdnative:
        # [general]
        # singleton=false
        # load_once=true
        # symbol_prefix="godot_"
        # reloadable=true
        # [entry]
        # X11.64="res://addons/godotsteam/x11/libgodotsteam.so"
        # Windows.64="res://addons/godotsteam/win64/godotsteam.dll"
        # OSX.64="res://addons/godotsteam/osx/libgodotsteam.dylib"
        # [dependencies]
        # X11.64=[ "res://addons/godotsteam/x11/libsteam_api.so" ]
        # Windows.64=[ "res://addons/godotsteam/win64/steam_api64.dll" ]
        # OSX.64=[ "res://addons/godotsteam/osx/libsteam_api.dylib" ]
        libraries = ini_file["entry"]
        bins = []
        for platform, lib in libraries.items():
            platform = platform.lower()
            lib = sanitize_lib(lib)
            bin = get_gdext_bin_info(platform, lib, addon_folder)
            bins.append(bin)
        version_info["bins"] = bins
        if "dependencies" in ini_file:
            deps = []
            dependencies = ini_file["dependencies"]
            for platform in dependencies:
                platform = platform.lower()
                lib_arr = dependencies.get(platform)
                # if lib_dict is a string, we have to parse it
                if isinstance(lib_arr, str):
                    lib_arr = json.loads(lib_arr)
                # lib is a dict
                for lib in lib_arr:
                    lib = sanitize_lib(lib)
                    bin = get_gdext_bin_info(platform, lib, addon_folder)
                    deps.append(bin)
            version_info["dependencies"] = deps
    else:
        # gdextension files go like this:
        # ```ini
        # [configuration]
        # entry_symbol = "godotsteam_init"
        # compatibility_minimum = 4.1

        # [libraries]
        # macos.debug = "osx/libgodotsteam.debug.framework"
        # macos.release = "osx/libgodotsteam.framework"
        # windows.debug.x86_64 = "win64/godotsteam.debug.x86_64.dll"
        # windows.debug.x86_32 = "win32/godotsteam.debug.x86_32.dll"
        # windows.release.x86_64 = "win64/godotsteam.x86_64.dll"
        # windows.release.x86_32 = "win32/godotsteam.x86_32.dll"
        # linux.debug.x86_64 = "linux64/libgodotsteam.debug.x86_64.so"
        # linux.debug.x86_32 = "linux32/libgodotsteam.debug.x86_32.so"
        # linux.release.x86_64 = "linux64/libgodotsteam.x86_64.so"
        # linux.release.x86_32 = "linux32/libgodotsteam.x86_32.so"

        # [dependencies]
        # windows.x86_64 = { "win64/steam_api64.dll": "" }
        # windows.x86_32 = { "win32/steam_api.dll": "" }
        # linux.x86_64 = { "linux64/libsteam_api.so": "" }
        # linux.x86_32 = { "linux32/libsteam_api.so": "" }
        # ```
        # get the configuration section
        configuration = ini_file["configuration"]
        if "compatibility_minimum" in configuration:
            version_info["min_godot_version"] = configuration.get("compatibility_minimum").strip('"')
            if (
                version_info["max_godot_version"] == ""
                or version_info["max_godot_version"] < version_info["min_godot_version"]
            ):
                version_info["max_godot_version"] = version_info["min_godot_version"]
        bins = []
        libraries = ini_file["libraries"]
        for platform, lib in libraries.items():
            lib = sanitize_lib(lib)
            bin = get_gdext_bin_info(platform, lib, addon_folder)
            bins.append(bin)
        version_info["bins"] = bins
        if "dependencies" in ini_file:
            deps = []
            dependencies = ini_file["dependencies"]
            for platform in dependencies:
                lib_dict = dependencies.get(platform)
                # if lib_dict is a string, we have to parse it
                if isinstance(lib_dict, str):
                    lib_dict = json.loads(lib_dict)
                # lib is a dict
                for lib, lib_remap in lib_dict.items():
                    # check if lib_remap exists
                    if lib_remap and os.path.exists(addon_folder + "/" + lib_remap.strip('"')):
                        lib = lib_remap
                    lib = sanitize_lib(lib)
                    bin = get_gdext_bin_info(platform, lib, addon_folder)
                    deps.append(bin)
            version_info["dependencies"] = deps
        else:
            version_info["dependencies"] = []
    return version_info


get_plugin_info("godot-jolt")
# get_plugin_info("godotsteam")
# trawl_asset_lib()
# write_plugin_versions()
# write_header_file()