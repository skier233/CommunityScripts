import os
import zipfile
from stashapi.stashapp import StashInterface
import stashapi.log as log
import config

tagid_cache = {}

ai_tag_ids_cache = set()

def initialize(connection):
    global stash
    global aierroed_tag_id
    global tagme_tag_id
    global ai_base_tag_id
    global ai_tagged_tag_id
    global vr_tag_id

    # Initialize the Stash API
    stash = StashInterface(connection)

    # Initialize "metadata" tags
    aierroed_tag_id = stash.find_tag(config.aierrored_tag_name, create=True)["id"]
    tagme_tag_id = stash.find_tag(config.tagme_tag_name, create=True)["id"]
    ai_base_tag_id = stash.find_tag(config.ai_base_tag_name, create=True)["id"]
    ai_tagged_tag_id = stash.find_tag(config.aitagged_tag_name, create=True)["id"]
    vr_tag_name = stash.get_configuration()["ui"].get("vrTag", None)
    if not vr_tag_name:
        log.warning("No VR tag found in configuration")
        vr_tag_id = None
    else:
        vr_tag_id = stash.find_tag(vr_tag_name)["id"]

# ----------------- Tag Methods -----------------

def get_tag_ids(tag_names, create=False):
        return [get_tag_id(tag_name, create) for tag_name in tag_names]

def get_tag_id(tag_name, create=False):
    if tag_name not in tagid_cache:
        stashtag = stash.find_tag(tag_name)
        if stashtag:
            tagid_cache[tag_name] = stashtag["id"]
            return stashtag["id"]
        else:
            if not create:
                return None
            tag = stash.create_tag({"name":tag_name, "ignore_auto_tag": True, "parent_ids":[ai_base_tag_id]})['id']
            tagid_cache[tag_name] = tag
            ai_tag_ids_cache.add(tag)
            return tag
    return tagid_cache.get(tag_name)

def get_ai_tags():
    if len(ai_tag_ids_cache) == 0:
        ai_tags = [item['id'] for item in stash.find_tags(f={"parents": {"value":1410, "modifier":"INCLUDES"}}, fragment="id")]
        ai_tag_ids_cache.update(ai_tags)
    else :
        ai_tags = list(ai_tag_ids_cache)
    return ai_tags

def is_scene_tagged(tags):
    for tag in tags:
        if tag['id'] == ai_tagged_tag_id:
            return True
    return False

def is_vr_scene(tags):
    for tag in tags:
        if tag['id'] == vr_tag_id:
            return True
    return False

# ----------------- Image Methods -----------------

def get_tagme_images():
    return stash.find_images(f={"tags": {"value":tagme_tag_id, "modifier":"INCLUDES"}}, fragment="id files {path}")

def add_error_images(image_ids):
    stash.update_images({"ids": image_ids, "tag_ids": {"ids": [aierroed_tag_id], "mode": "ADD"}})

def remove_tagme_tags_from_images(image_ids):
    stash.update_images({"ids": image_ids, "tag_ids": {"ids": [tagme_tag_id], "mode": "REMOVE"}})

def remove_ai_tags_from_images(image_ids, remove_tagme=True, remove_errored=True):
    ai_tags = get_ai_tags()
    if remove_tagme:
        ai_tags.append(tagme_tag_id)
    if remove_errored:
        ai_tags.append(aierroed_tag_id)
    stash.update_images({"ids": image_ids, "tag_ids": {"ids": ai_tags, "mode": "REMOVE"}})

def add_tags_to_image(image_id, tag_ids):
    stash.update_images({"ids": [image_id], "tag_ids": {"ids": tag_ids, "mode": "ADD"}})

worker_counter = 0

def get_image_paths_and_ids(images):
    global worker_counter
    counter_updated = False
    imagePaths = []
    imageIds = []
    temp_files = []
    for image in images:
        try:
            imagePath = image['files'][0]['path']
            imageId = image['id']
            if '.zip' in imagePath:
                if not counter_updated:
                    worker_counter += 1
                    counter_updated = True
                zip_index = imagePath.index('.zip') + 4
                zip_path, img_path = imagePath[:zip_index], imagePath[zip_index+1:].replace('\\', '/')

                 # Create a unique temporary directory for this worker
                temp_dir = os.path.join(config.temp_image_dir, f"worker_{worker_counter}")
                os.makedirs(temp_dir, exist_ok=True)
                
                temp_path = os.path.join(temp_dir, img_path)
                os.makedirs(os.path.dirname(temp_path), exist_ok=True)

                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extract(img_path, temp_dir)
                    imagePath = os.path.abspath(os.path.normpath(temp_path))
                
                temp_files.append(temp_path)
                temp_files.append(temp_dir)  # Ensure the temp directory is also added to temp_files
            imagePaths.append(imagePath)
            imageIds.append(imageId)
        except IndexError:
            log.error(f"Failed to process image: {image}")
            continue
    return imagePaths, imageIds, temp_files

# ----------------- Scene Methods -----------------

def add_tags_to_video(video_id, tag_ids, add_tagged=True):
    if add_tagged:
        tag_ids.append(ai_tagged_tag_id)
    stash.update_scenes({"ids": [video_id], "tag_ids": {"ids": tag_ids, "mode": "ADD"}})

def remove_ai_tags_from_video(video_id, remove_tagme=True, remove_errored=True):
    ai_tags = get_ai_tags()
    if remove_tagme:
        ai_tags.append(tagme_tag_id)
    if remove_errored:
        ai_tags.append(aierroed_tag_id)
    stash.update_scenes({"ids": [video_id], "tag_ids": {"ids": ai_tags, "mode": "REMOVE"}})

def get_tagme_scenes():
    return stash.find_scenes(f={"tags": {"value":tagme_tag_id, "modifier":"INCLUDES"}}, fragment="id tags {id} files {path duration fingerprint(type: \"phash\")}")

def add_error_scene(scene_id):
    stash.update_scenes({"ids": [scene_id], "tag_ids": {"ids": [aierroed_tag_id], "mode": "ADD"}})

def remove_tagme_tag_from_scene(scene_id):
    stash.update_scenes({"ids": [scene_id], "tag_ids": {"ids": [tagme_tag_id], "mode": "REMOVE"}})

# ----------------- Marker Methods -----------------

def add_markers_to_video_from_dict(video_id, tag_timespans_dict):
    for _, tag_timespan_dict in tag_timespans_dict.items():
        for tag_name, time_frames in tag_timespan_dict.items():
            tag_id = get_tag_id(tag_name, create=True)
            add_markers_to_video(video_id, tag_id, tag_name, time_frames)

def add_markers_to_video(video_id, tag_id, tag_name, time_frames):
    for time_frame in time_frames:
        #TODO: only use end_seconds if on the dev version of stash
        stash.create_scene_marker({"scene_id": video_id, "primary_tag_id":tag_id, "tag_ids": [tag_id], "seconds": time_frame.start, "end_seconds": time_frame.end, "title":tag_name})

def remove_ai_markers_from_video(video_id):
    ai_tags = set(get_ai_tags())
    scene_markers = stash.get_scene_markers(video_id, fragment="id primary_tag {id}")
    for scene_marker in scene_markers:
        if scene_marker.get("primary_tag").get("id") in ai_tags:
            stash.destroy_scene_marker(scene_marker.get("id"))