import asyncio
from collections import namedtuple
from dataclasses import dataclass
from enum import Enum
import gzip
import logging
import subprocess
from typing import Literal, Mapping, Optional, Sequence
from fastapi.datastructures import Headers
from fastapi.responses import Response, StreamingResponse
from cachetools import TTLCache

from fastapi import FastAPI, Request

SYMREF_PREFIX=b"ref: "
EXTENSIONS=\
    b"multi_ack thin-pack side-band side-band-64k ofs-delta shallow " \
    b"deepen-since deepen-not deepen-relative no-progress include-tag " \
    b"multi_ack_detailed no-done object-format=sha1 agent=git/2.30.2"
HEADERS = {
    "Expires": "Fri, 01 Jan 1980 00:00:00 GMT",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache, max-age=0, must-revalidate",
}
GIT_PATH="/usr/bin/git"
GIT_HTTP_BACKEND="/Library/Developer/CommandLineTools/usr/libexec/git-core/git-http-backend"
GIT_PROJECT_ROOT="/Users/graham/Developer/github.com"

logger = logging.getLogger(__name__)

app = FastAPI()

class PktLineConstants(Enum):
    FLUSH = 'FLUSH'

@dataclass(frozen=True)
class PktLineData:
    data: bytes

PktLine = PktLineData | Literal[PktLineConstants.FLUSH]


def encode_pkt_line(pkt: PktLine) -> bytes:
    if pkt == PktLineConstants.FLUSH:
        return b'0000'
    length_prefix = f'{len(pkt.data) + 4:04x}'.encode('ascii')
    return length_prefix + pkt.data

def parse_pkt_lines(data: bytes) -> tuple[list[PktLine], bytes]:
    pkts = []
    offset = 0
    size = len(data)
    while True:
        if size - offset < 4:
            return pkts, data[offset:]
        pkt_length_prefix = data[offset:(offset + 4)]
        pkt_length = int(pkt_length_prefix, 16)
        if pkt_length == 0:
            pkts.append(PktLineConstants.FLUSH)
            offset += 4
        else:
            if size - offset < pkt_length:
                return pkts, data[offset:]
            pkts.append(PktLineData(data[offset + 4:(offset + pkt_length)]))
            offset += pkt_length

SymRef = namedtuple('SymRef', ['target', 'ref'])
ResolvedRef = namedtuple('ResolvedRef', ['objid', 'ref'])


def parse_refs(data: bytes) -> tuple[Sequence[SymRef], Sequence[ResolvedRef]]:
    sym_refs: list[SymRef] = []
    resolved_refs: list[ResolvedRef] = []
    for line in map(bytes.rstrip, data.splitlines()):
        left, ref = line.split(b'\t', maxsplit=2)
        if left.startswith(SYMREF_PREFIX):
            sym_refs.append(SymRef(left[len(SYMREF_PREFIX):], ref))
        else:
            resolved_refs.append(ResolvedRef(left, ref))
    return sym_refs, resolved_refs

_CACHED_REPO_REFERENCES = TTLCache(maxsize=32, ttl=5*60)
_CACHED_REFS = TTLCache(maxsize=1024, ttl=10*60)

@dataclass(frozen=True)
class RepoKey:
    organisation: str
    repo: str

@app.get("/github.com/{organisation}/{repo}/info/refs")
async def list_references(organisation: str, repo: str, request: Request, service: Optional[str] = None) -> Response:
    logger.info(f"Headers: {request.headers}")
    repo_key = RepoKey(organisation=organisation, repo=repo)
    content = _CACHED_REPO_REFERENCES.get(repo_key)
    if content is None:
        git_process = await asyncio.create_subprocess_exec(
            GIT_PATH, "ls-remote", "--symref", f"git@github.com:{organisation}/{repo}",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, _stderr = await git_process.communicate()

        sym_refs, resolved_refs = parse_refs(stdout)
        result_pkts: list[PktLine] = []
        result_pkts.append(PktLineData(b"# service=git-upload-pack\n"))
        result_pkts.append(PktLineConstants.FLUSH)
        for idx, resolved_ref in enumerate(resolved_refs):
            line = b'%b %b' % (resolved_ref.objid, resolved_ref.ref)
            if idx == 0:
                line += b'\0' + EXTENSIONS
                for sym_ref in sym_refs:
                    line += b" symref=%b:%b" % (sym_ref.ref, sym_ref.target)
            line += b'\n'
            result_pkts.append(PktLineData(line))
        result_pkts.append(PktLineConstants.FLUSH)
        content = b''.join(encode_pkt_line(pkt) for pkt in result_pkts)
        _CACHED_REPO_REFERENCES[repo_key] = content
        _CACHED_REFS[repo_key] = {
            resolved_ref.objid: resolved_ref.ref for resolved_ref in resolved_refs
        }

    return Response(
        headers=HEADERS,
        media_type="application/x-git-upload-pack-advertisement",
        content=content,
    )

async def decode_body(request: Request) -> bytes:
    raw_body = await request.body()
    content_encoding = request.headers.get('content-encoding')
    if content_encoding is not None and 'gzip' in content_encoding:
        return gzip.decompress(raw_body)
    return raw_body

def headers_to_env(headers: Headers) -> Mapping[str, str]:
    return {
        'HTTP_' + k.replace('-', '_').upper(): v for k, v in headers.items()
    } | { 'CONTENT_TYPE': headers.get('content-type'), 'HTTP_CONTENT_ENCODING': '' }

def parse_headers(raw_headers: bytes) -> Headers:
    parsed_headers: list[tuple[bytes, bytes]] = [
        line.split(b': ', maxsplit=2) for line in raw_headers.split(b'\r\n')
    ]
    return Headers(raw=parsed_headers)



@app.post("/github.com/{organisation}/{repo}/git-upload-pack")
async def git_upload_pack(organisation: str, repo: str, request: Request) -> Response:
    body = await decode_body(request)
    pkts, remainder = parse_pkt_lines(body)

    if len(remainder) > 0:
        raise Exception(f"Buffer is expected to be empty but is {remainder}")

    logger.info(f"Request headers: {request.headers}")
    logger.info(f"Parsed pkts: {pkts}")
    known_refs: Mapping[bytes, bytes] = _CACHED_REFS[RepoKey(organisation=organisation, repo=repo)]
    ref_wants: list[str] = []
    for pkt in pkts:
        if not isinstance(pkt, PktLineData):
            continue
        if not pkt.data.startswith(b'want '):
            continue
        want, *_ = pkt.data.removeprefix(b'want ').split(b' ', 2)
        want = want.strip()
        ref = known_refs[want]
        ref_wants.append(ref.decode('ascii'))
    git_fetch_process = await asyncio.create_subprocess_exec(
        GIT_PATH,
        f"--git-dir={GIT_PROJECT_ROOT}/{organisation}/{repo}",
        "fetch",
        "origin",
        "--no-show-forced-updates",
        *ref_wants,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return_code = await git_fetch_process.wait()
    if return_code != 0:
        raise Exception(f'git fetch terminated with non-zero result: {return_code}')

    env = {
            "REQUEST_METHOD": "POST",
            "GIT_PROJECT_ROOT": GIT_PROJECT_ROOT,
            "PATH_INFO": f'/{organisation}/{repo}/git-upload-pack'
        } | headers_to_env(request.headers)
    git_process = await asyncio.create_subprocess_exec(
        GIT_HTTP_BACKEND,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE
    )
    git_process.stdin.write(body)
    await git_process.stdin.drain()
    git_process.stdin.close()
    await git_process.stdin.wait_closed()
    body = git_process.stdout
    raw_headers = await body.readuntil(b'\r\n\r\n')
    logger.info(f"Raw headers: {raw_headers}")
    parsed_headers = parse_headers(raw_headers.removesuffix(b'\r\n\r\n'))
    logger.info(f"Parsed headers: {parsed_headers}")

    return StreamingResponse(body, media_type='application/x-git-upload-pack-result', headers=parsed_headers)
