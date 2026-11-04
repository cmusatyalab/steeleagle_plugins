DEFAULT_VIDEO_PATH = "/home/particle/uriel/patchcore-pipeline/test.mp4"
DEFAULT_FPS = 30


def main():
    """Runs the driver with the args and listen socket the daemon gives it."""
    args = json.loads(os.getenv("STEELEAGLE_PLUGIN_ARGS") or "{}")
    video_path = args.get("video_path") or DEFAULT_VIDEO_PATH
    fps = args.get("fps") or DEFAULT_FPS

    listen_address = os.getenv("STEELEAGLE_LISTEN_SOCKET")
    if not listen_address:
        raise ValueError("no listen socket address provided!")
    Path(listen_address).unlink(missing_ok=True)

    logger.info(f"reading from {video_path} at {fps} fps")

    server = grpc.server(thread_pool=futures.ThreadPoolExecutor(max_workers=10))
    add_StreamServiceServicer_to_server(Stream(video_path, fps), server)
    server.add_insecure_port(f"unix://{listen_address}")
    server.start()
    logger.info("all services started")
    server.wait_for_termination()
