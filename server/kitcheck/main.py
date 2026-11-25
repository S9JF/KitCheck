"""FastAPI entry point: uvicorn kitcheck.main:app --host 0.0.0.0 --port 8080"""
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .api import get_api

api = get_api()
app = FastAPI(title="KitCheck", docs_url=None, redoc_url=None, openapi_url=None)


def _close_soon(it):
    """Close a sync generator from another thread; retry while a worker thread is still inside next()."""
    def run():
        for _ in range(100):
            try:
                it.close()
                return
            except ValueError:  # "generator already executing"
                time.sleep(0.1)
    threading.Thread(target=run, daemon=True).start()


async def _pump(it):
    # Advance the blocking frame generator in the threadpool; when the viewer disconnects this async
    # generator is cancelled/finalised and the upstream camera connection is closed right away.
    try:
        while True:
            chunk = await run_in_threadpool(next, it, None)
            if chunk is None:
                return
            yield chunk
    finally:
        _close_soon(it)


@app.get("/api/camera/stream")
@app.get("/api/camera/small/stream")
async def camera_stream(request: Request):
    # Long-lived MJPEG response, so it cannot go through api.handle (which returns a whole body).
    # /small/stream: tray-only, downscaled frames with CiRA boxes per part, for the ESP32 kiosk screen.
    status, headers, body = await run_in_threadpool(
        api.camera_stream,
        {k.lower(): v for k, v in request.headers.items()},
        dict(request.query_params),
        request.url.path.endswith("/small/stream"),
    )
    if isinstance(body, bytes):
        return Response(content=body, status_code=status, headers=headers)
    media = headers.pop("Content-Type")
    return StreamingResponse(_pump(body), status_code=status, headers=headers, media_type=media)


@app.get("/api/virtual/stream")
async def virtual_stream(request: Request):
    # The /sim page's virtual tray as an MJPEG camera, for the AI station (gpu_station relay -> CiRA GstCam).
    status, headers, body = await run_in_threadpool(api.virtual_stream, request.client.host if request.client else "")
    if isinstance(body, bytes):
        return Response(content=body, status_code=status, headers=headers)
    media = headers.pop("Content-Type")
    return StreamingResponse(_pump(body), status_code=status, headers=headers, media_type=media)


@app.api_route("/{path:path}", methods=["GET", "POST", "PATCH"])
async def route(path: str, request: Request):
    body = await request.body()
    status, headers, data = await run_in_threadpool(
        api.handle,
        request.method,
        "/" + path,
        dict(request.query_params),
        {k.lower(): v for k, v in request.headers.items()},
        body,
        request.client.host if request.client else "",
    )
    return Response(content=data, status_code=status, headers=headers)
