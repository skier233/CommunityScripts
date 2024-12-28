import os
import sys
import json
import subprocess
import shutil
import traceback
# ----------------- Setup -----------------

def install(package):
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])
    except Exception as e:
        log.error(f"Failed to install {package}: {e}. If you're running in docker or a" + 
                   "venv you may need to pip install dependencies manually using the provided requirements.txt")
        raise Exception(f"Failed to install {package}")

try:
    toRaise = False
    try:
        import stashapi.log as log
        from stashapi.stashapp import StashInterface
    except ModuleNotFoundError:
        install('stashapp-tools')
        toRaise = True

    try:
        import aiohttp
    except ModuleNotFoundError:
        install('aiohttp')
        toRaise = True

    try:
        import asyncio
    except ModuleNotFoundError:
        install('asyncio')
        toRaise = True
    
    try:
        import pydantic
    except ModuleNotFoundError:
        install('pydantic')
        toRaise = True
        
    if toRaise:
        raise Exception("Installed required packages, please retry the task.")
    
    try:
        import config
    except ModuleNotFoundError:
        log.error("Please provide a config.py file with the required variables.")
        raise Exception("Please provide a config.py file with the required variables.")
    import media_handler
    import ai_server
    import utility
except:
    log.error("Attempted to install required packages, please retry the task.")
    sys.exit(1)
    raise
        
# ----------------- Variable Definitions -----------------

semaphore = asyncio.Semaphore(config.CONCURRENT_TASK_LIMIT)
progress = 0
increment = 0.0

# ----------------- Main Execution -----------------

async def main():
    json_input = read_json_input()
    output = {}
    await run(json_input, output)
    out = json.dumps(output)
    print(out + "\n")

def read_json_input():
    json_input = sys.stdin.read()
    return json.loads(json_input)

async def run(json_input, output):
    PLUGIN_ARGS = False
    try:
        log.debug(json_input["server_connection"])
        os.chdir(json_input["server_connection"]["PluginDir"])
        media_handler.initialize(json_input["server_connection"])
    except Exception:
        raise

    try:
        PLUGIN_ARGS = json_input['args']["mode"]
    except:
        pass
    if PLUGIN_ARGS == "tag_images":
        await tag_images()
        output["output"] = "ok"
        return
    elif PLUGIN_ARGS == "tag_scenes":
        await tag_scenes()
        output["output"] = "ok"
        return
    output["output"] = "ok"
    return

# ----------------- High Level Calls -----------------

async def tag_images():
    global increment
    images = media_handler.get_tagme_images()
    if images:
        image_batches = [images[i:i + config.IMAGE_REQUEST_BATCH_SIZE] for i in range(0, len(images), config.IMAGE_REQUEST_BATCH_SIZE)]
        increment = 1.0 / len(image_batches)
        tasks = [__tag_images(batch) for batch in image_batches]
        await asyncio.gather(*tasks)
    else:
        log.info("No images to tag. Have you tagged any images with the AI_TagMe tag to get processed?")


async def tag_scenes():
    global increment
    scenes = media_handler.get_tagme_scenes()
    if scenes:
        increment = 1.0 / len(scenes)
        tasks = [__tag_scene(scene) for scene in scenes]
        await asyncio.gather(*tasks)
    else:
        log.info("No scenes to tag. Have you tagged any scenes with the AI_TagMe tag to get processed?")

# ----------------- Image Processing -----------------

async def __tag_images(images):
    async with semaphore:
        imagePaths, imageIds, temp_files = media_handler.get_image_paths_and_ids(images)
        imagePaths = [utility.mutate_path(path) for path in imagePaths]
        try:
            server_result = await ai_server.process_images_async(imagePaths)
            if server_result is None:
                log.error("Server returned no results")
                media_handler.add_error_images(imageIds)
                media_handler.remove_tagme_tags_from_images(imageIds)
                return
            server_results = ai_server.ImageResult(**server_result)
            results = server_results.result
            if len(results) != len(imageIds):
                log.error("Server returned incorrect number of results")
                media_handler.add_error_images(imageIds)
            else:
                media_handler.remove_ai_tags_from_images(imageIds, remove_tagme=False)

                for id, result in zip(imageIds, results):
                    if 'error' in result:
                        log.error(f"Error processing image: {result['error']}")
                        media_handler.add_error_images([id])
                    else:
                        tags_list = []
                        for _, tags in result.items():
                            stashtag_ids = media_handler.get_tag_ids(tags)
                            stashtag_ids.append(media_handler.ai_tagged_tag_id)
                            tags_list.extend(stashtag_ids)
                        media_handler.add_tags_to_image(id, tags_list)

            log.info(f"Tagged {len(imageIds)} images")
            media_handler.remove_tagme_tags_from_images(imageIds)
        except aiohttp.ClientConnectionError as e:
            log.error(f"Failed to connect to AI server. Is the AI server running at {config.API_BASE_URL}?   {e}")
        except asyncio.TimeoutError as a:
            log.error(f"Timeout processing images: {a}")
        except Exception as e:
            log.error(f"Failed to process images: {e}Stack trace: {traceback.format_exc()}")
            media_handler.add_error_images(imageIds)
            media_handler.remove_tagme_tags_from_images(imageIds)
        finally:
            increment_progress()
            for temp_file in temp_files:
                try:
                    if os.path.isdir(temp_file):
                        shutil.rmtree(temp_file)
                    else:
                        os.remove(temp_file)
                except Exception as e:
                    log.debug(f"Failed to remove temp file {temp_file}: {e}")


# ----------------- Scene Processing -----------------
async def __tag_scene(scene):
    async with semaphore:
        scenePath = scene['files'][0]['path']
        sceneId = scene['id']
        duration = scene['files'][0].get('duration', None)
        log.debug("files result:" + str(scene['files'][0]))
        if duration is None:
            log.error(f"Scene {sceneId} has no duration")
            return

        mutated_path = utility.mutate_path(scenePath)

        try:
            already_ai_tagged = media_handler.is_scene_tagged(scene.get('tags'))
            ai_file_path = scenePath + ".AI.json"
            saved_json = None
            if already_ai_tagged:
                if os.path.exists(ai_file_path):
                    try:
                        saved_json = utility.read_json_from_file(ai_file_path)
                    except Exception as e:
                        log.error(f"Failed to load AI results from file: {e}")
                else:
                    log.warning(f"Scene {scenePath} is already tagged but has no AI results file. Running AI again.")
            vr_video = media_handler.is_vr_scene(scene.get('tags'))
            if vr_video:
                log.info(f"Processing VR video {scenePath}")
            server_result = await ai_server.process_video_async(video_path=mutated_path, vr_video=vr_video, existing_json=saved_json)

            if server_result is None:
                log.error("Server returned no results")
                media_handler.add_error_scene(sceneId)
                media_handler.remove_tagme_tag_from_scene(sceneId)
                return
            server_result = ai_server.VideoResult(**server_result)

            result = server_result.result
            json_to_write = result['json_result']
            if json_to_write:
                utility.write_json_to_file(ai_file_path, json_to_write)
            video_tag_info = ai_server.VideoTagInfo(**result['video_tag_info'])

            media_handler.remove_ai_tags_from_video(sceneId, remove_tagme=True)
            allTags = []
            for _, tag_set in video_tag_info.video_tags.items():
                allTags.extend(tag_set)
            tagIdsToAdd = media_handler.get_tag_ids(allTags, create=True)
            media_handler.add_tags_to_video(sceneId, tagIdsToAdd)

            #TODO: find a good place to store total durations of tags in a video and ideally be able to query them and see them in stash's UI (via custom plugin db fields?)
            todo = video_tag_info.tag_totals

            if config.CREATE_MARKERS:
                media_handler.remove_ai_markers_from_video(sceneId)
                media_handler.add_markers_to_video_from_dict(sceneId, video_tag_info.tag_timespans)
            log.info(f"Server Result: {server_result}")
            log.info(f"Processed video with {len(server_result.result)} AI tagged frames")
        except aiohttp.ClientConnectionError as e:
            log.error(f"Failed to connect to AI server. Is the AI server running at {config.API_BASE_URL}?   {e}")
        except asyncio.TimeoutError as a:
            log.error(f"Timeout processing scene: {a}")
        except Exception as e:
            log.error(f"Failed to process video: {e}\n{traceback.format_exc()}")
            media_handler.add_error_scene(sceneId)
            media_handler.remove_tagme_tag_from_scene(sceneId)
            return
        finally:
            increment_progress()
  
# ----------------- Utility Functions -----------------

def increment_progress():
    global progress
    global increment
    progress += increment
    log.progress(progress)
asyncio.run(main())