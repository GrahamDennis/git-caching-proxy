import asyncio
from dataclasses import dataclass
from enum import Enum
import gzip
import logging
import subprocess
from typing import Annotated, Optional, Sequence
from fastapi.responses import Response, StreamingResponse
from starlette.convertors import Convertor, register_url_convertor
import os

from fastapi import Depends, FastAPI, HTTPException, Header, Request

NO_CACHE_HEADERS = {
    "Expires": "Fri, 01 Jan 1980 00:00:00 GMT",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache, max-age=0, must-revalidate",
}
GIT_PATH="/usr/bin/git"
GIT_PROJECT_ROOT="/Users/graham/Developer/github.com"

logger = logging.getLogger(__name__)

class GitRepoConvertor(Convertor[str]):
    regex = ".*\.git(?=$|/)"

    def convert(self, value: str) -> str:
        return str(value)

    def to_string(self, value: str) -> str:
        return str(value)

register_url_convertor("git_repo", GitRepoConvertor())


async def verify_git_protocol_version(git_protocol: Annotated[Optional[bytes], Header()] = None) -> None:
    if git_protocol != b'version=2':
        raise HTTPException(status_code=400, detail=f"git protocol '{git_protocol}' unsupported.")


app = FastAPI(dependencies=[Depends(verify_git_protocol_version)])

class PktLineConstants(Enum):
    FLUSH = 0
    DELIMITER = 1
    RESPONSE_END = 2
    INVALID = 3

@dataclass(frozen=True)
class PktLineData:
    data: bytes

PktLine = PktLineData | PktLineConstants


def parse_pkt_lines(data: bytes) -> tuple[list[PktLine], bytes]:
    pkts = []
    offset = 0
    size = len(data)
    while True:
        if size - offset < 4:
            return pkts, data[offset:]
        pkt_length_prefix = data[offset:(offset + 4)]
        pkt_length = int(pkt_length_prefix, 16)
        if pkt_length < 4:
            pkts.append(PktLineConstants(pkt_length))
            offset += 4
        else:
            if size - offset < pkt_length:
                return pkts, data[offset:]
            pkts.append(PktLineData(data[offset + 4:(offset + pkt_length)]))
            offset += pkt_length

def path_to_repo(repo: str) -> str:
    return f'{GIT_PROJECT_ROOT}/{repo}'

async def git_init_if_required(repo: str, repo_path: str) -> None:
    if os.path.isdir(repo_path):
        return
    git_clone_process = await asyncio.create_subprocess_exec(
        GIT_PATH,
        "clone",
        '--quiet',
        '--mirror',
        '--single-branch',
        f'git@github.com:{repo}',
        repo_path,
    )
    git_clone_return_code = await git_clone_process.wait()
    if not git_clone_return_code == 0:
        raise Exception(f"git clone failed with return code {git_clone_return_code}")



@app.get("/github.com/{repo:git_repo}/info/refs")
async def git_info_refs(repo: str, service: Optional[bytes] = None) -> Response:
    if not service == b'git-upload-pack':
        raise HTTPException(status_code=400, detail=f"Unsupported service '{service}'")
    repo_path = path_to_repo(repo)
    await git_init_if_required(repo, repo_path)
    return await proxy_to_git("upload-pack", "--http-backend-info-refs", repo_path)


async def decode_body(request: Request) -> bytes:
    raw_body = await request.body()
    content_encoding = request.headers.get('content-encoding')
    if content_encoding is not None and 'gzip' in content_encoding:
        return gzip.decompress(raw_body)
    return raw_body

async def proxy_to_git(service_name: str, *arguments: str) -> Response:
    git_process = await asyncio.create_subprocess_exec(
        GIT_PATH,
        service_name,
        *arguments,
        stdout=subprocess.PIPE,
        env={
            'GIT_PROTOCOL': 'version=2',
        }
    )
    return StreamingResponse(
        git_process.stdout,
        media_type=f'application/x-git-{service_name}-advertisement',
        headers=NO_CACHE_HEADERS
    )

def get_git_command(pkt: PktLine) -> bytes:
    if not isinstance(pkt, PktLineData):
        raise Exception(f"Expected a PktLineData, but received '{pkt}'")
    if not pkt.data.startswith(b'command='):
        raise Exception(f"Expected a command PktLineData but received '{pkt}'")
    return pkt.data.removeprefix(b'command=').rstrip(b'\n')

def get_refspecs(pkts: Sequence[PktLine]) -> list[bytes]:
    result: list[bytes] = []
    for pkt in pkts:
        if not isinstance(pkt, PktLineData):
            continue
        if not pkt.data.startswith(b'ref-prefix '):
            continue
        ref_prefix = pkt.data.removeprefix(b'ref-prefix ').rstrip()
        result.append(b'%b*:%b*' % (ref_prefix, ref_prefix))
    return result
        
async def update_refs(repo_path: str, refspecs: list[bytes]) -> None:
    logger.warning(f"Fetching refspecs: {refspecs}")
    git_process = await asyncio.create_subprocess_exec(
        GIT_PATH,
        f"--git-dir={repo_path}",
        "fetch",
        "origin",
        '--quiet',
        '--no-write-fetch-head',
        '--no-show-forced-updates',
        '--stdin',
        stderr=subprocess.DEVNULL,
        stdin=subprocess.PIPE,
    )
    git_process.stdin.write(b'\n'.join(refspecs))
    await git_process.stdin.drain()
    git_process.stdin.close()
    await git_process.stdin.wait_closed()
    return_code = await git_process.wait()
    if return_code != 0:
        raise Exception(f'git fetch terminated with non-zero result {return_code}')


@app.post("/github.com/{repo:git_repo}/git-upload-pack")
async def git_upload_pack(repo: str, request: Request) -> Response:
    repo_path = path_to_repo(repo)
    body = await decode_body(request)
    logger.info(f"Request headers: {request.headers}")
    logger.info(f"Raw body: {body}")
    pkts, remainder = parse_pkt_lines(body)

    if len(remainder) > 0:
        raise Exception(f"Buffer is expected to be empty but is {remainder}")

    logger.info(f"Parsed pkts: {pkts}")

    command = get_git_command(pkts[0])
    logger.info(f'Command: {command}')
    
    if command == b'ls-refs':
        # Consider a cache before refetching
        refspecs = get_refspecs(pkts)
        if len(refspecs) > 0:
            await update_refs(repo_path, refspecs)

    git_process = await asyncio.create_subprocess_exec(
        GIT_PATH,
        'upload-pack',
        '--stateless-rpc',
        repo_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env={
            'GIT_PROTOCOL': 'version=2',
        }
    )

    git_process.stdin.write(body)
    await git_process.stdin.drain()
    git_process.stdin.close()
    await git_process.stdin.wait_closed()
    return StreamingResponse(
        git_process.stdout,
        media_type='application/x-git-upload-pack-result',
        headers=NO_CACHE_HEADERS
    )

