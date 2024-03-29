from http import HTTPStatus
import logging
from typing import cast
from urllib.parse import urlparse

from fastapi import Request, Response, HTTPException
from fastapi.responses import JSONResponse

import httpx
import requests
import requests.exceptions
import yaml

from open_science_catalog_backend import app, config

logger = logging.getLogger(__name__)

ALL_METHODS = ["GET", "POST", "PUT", "DELETE", "HEAD"]

URL_PREFIX = "/processing/{remote_backend}/"


def remote_backend_to_url(remote_backend: str) -> str:
    mapping = cast(dict, config.REMOTE_PROCESSING_BACKEND_MAPPING)

    try:
        return mapping[remote_backend]
    except KeyError:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail=f"Invalid remote backend {remote_backend}",
        )


def requests_response_to_fastapi_response(response) -> Response:
    excluded_headers = [
        "content-encoding",
        "content-length",
        "transfer-encoding",
        "connection",
    ]
    headers = {
        name: value
        for (name, value) in response.raw.headers.items()
        if name.lower() not in excluded_headers
    }
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=headers,
    )


async def handle_proxy_request(
    request: Request,
    remote_backend: str,
    service_prefix: str,
    proxy_path: str = "",
):
    url = (
        remote_backend_to_url(remote_backend)
        + "/"
        + service_prefix
        + (f"/{proxy_path}" if proxy_path else "")
    )

    remote_backend_host = cast(str, urlparse(url).hostname)

    headers = dict(request.headers) | {"Host": remote_backend_host}

    proxy_kwargs = {
        "url": url,
        "params": dict(request.query_params),
        "headers": headers,
        "data": await request.body(),
        "allow_redirects": False,
    }

    logger.info(f"Requested {request.url}")
    logger.info(str(proxy_kwargs))
    logger.info(f"Proxying to {proxy_kwargs['url']}")
    response = getattr(requests, request.method.lower())(**proxy_kwargs)
    logger.info(f"Got status {response.status_code} and size {len(response.content)}")
    logger.info(response.content.decode()[:500])
    # NOTE: don't raise for status, but forward errors
    return requests_response_to_fastapi_response(response)


def generate_reverse_proxy(
    service_prefix: str,
):
    async def do_handle_proxy_request(
        request: Request,
        remote_backend: str,
        proxy_path: str = "",
    ):
        return await handle_proxy_request(
            request=request,
            remote_backend=remote_backend,
            proxy_path=proxy_path,
            service_prefix=service_prefix,
        )

    # need to change name to avoid confusion
    do_handle_proxy_request.__name__ = service_prefix + "_service"

    app.api_route(
        URL_PREFIX + service_prefix,
        methods=ALL_METHODS,
    )(do_handle_proxy_request)

    app.api_route(
        URL_PREFIX + service_prefix + "/{proxy_path:path}",
        methods=ALL_METHODS,
    )(do_handle_proxy_request)


@app.post(URL_PREFIX + "processes/{process}/execution")
async def execute_process(
    request: Request, remote_backend: str, process: str
) -> Response:
    logger.info("Deploying process {process}")
    # we always need to deploy the process in order to get its ID
    process_location = await deploy_process(
        request=request,
        remote_backend=remote_backend,
        process=process,
    )

    logger.info(f"Got remote process location {process_location}")

    process_id = process_location.split("/")[-1]

    logger.info(f"Extracted remote process id {process_id}")

    return await handle_proxy_request(
        request=request,
        remote_backend=remote_backend,
        service_prefix="processes",
        proxy_path=f"{process_id}/execution",
    )


async def deploy_process(request: Request, remote_backend: str, process: str) -> str:
    cwl_link = await _fetch_cwl_link_from_catalog(process)

    async with httpx.AsyncClient() as client:
        url = remote_backend_to_url(remote_backend) + "/processes"
        logger.info(f"Deploying process at {url}")
        deploy_response = await client.post(
            url,
            # NOTE: only forward auth here
            headers={"X-User-Id": request.headers["x-user-id"]},
            json={
                "inputs": {
                    "applicationPackage": {
                        "href": cwl_link,
                        "type": "application/cwl",
                    }
                }
            },
        )
    logger.info(f"Process deploy response: {deploy_response.content.decode()[:1000]}")
    deploy_response.raise_for_status()
    return deploy_response.headers["Location"]


async def _do_download(url, **kwargs):
    async with httpx.AsyncClient() as client:
        response = await client.get(url, **kwargs)
        response.raise_for_status()
    return response


async def _fetch_cwl_link_from_catalog(process: str) -> str:
    logger.info(f"Fetching catalog from {config.RESOURCE_CATALOG_METADATA_URL}")
    catalog_response = await _do_download(
        f"{config.RESOURCE_CATALOG_METADATA_URL}/{process}",
        params={"f": "json"},
    )
    (link,) = [
        link for link in catalog_response.json()["links"] if link["rel"] == "manifest"
    ]
    return link['href']


generate_reverse_proxy(service_prefix="processes")
generate_reverse_proxy(service_prefix="jobs")


@app.get("/applications/{application}")
async def get_application(application: str):
    cwl_link = await _fetch_cwl_link_from_catalog(application)
    logger.info(f"Fetching cwl from {cwl_link}")
    cwl_response = await _do_download(cwl_link)
    return JSONResponse(yaml.safe_load(cwl_response.content))
