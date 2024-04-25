#!/usr/bin/env python3

import sys
from collections import namedtuple
import typing as t

SYMREF_PREFIX="ref: "

HEADER_LINES=[
    "Expires: Fri, 01 Jan 1980 00:00:00 GMT",
    "Pragma: no-cache",
    "Cache-Control: no-cache, max-age=0, must-revalidate",
    "Content-Type: application/x-git-upload-pack-advertisement",
    ""
]

EXTENSIONS="multi_ack thin-pack side-band side-band-64k ofs-delta shallow deepen-since deepen-not deepen-relative no-progress include-tag multi_ack_detailed no-done object-format=sha1 agent=git/2.30.2"

def print_header():
    for line in HEADER_LINES:
        print(line, end='\r\n')

def encode_pkt_line(line: str) -> str:
    length_prefix = f'{len(line) + 4:04x}'
    return length_prefix + line

SymRef = namedtuple('SymRef', ['target', 'ref'])
ResolvedRef = namedtuple('ResolvedRef', ['objid', 'ref'])

def parse_refs(f: t.TextIO) -> tuple[t.Sequence[SymRef], t.Sequence[ResolvedRef]]:
    sym_refs: list[SymRef] = []
    resolved_refs: list[ResolvedRef] = []
    for line in map(str.rstrip, f):
        left, ref = line.split('\t', maxsplit=2)
        if left.startswith(SYMREF_PREFIX):
            sym_refs.append(SymRef(left[len(SYMREF_PREFIX):], ref))
        else:
            resolved_refs.append(ResolvedRef(left, ref))
    return sym_refs, resolved_refs



def run():
    print_header()
    sym_refs, resolved_refs = parse_refs(sys.stdin)
    print(encode_pkt_line("# service=git-upload-pack\n"), end='')
    print("0000", end='')
    for idx, resolved_ref in enumerate(resolved_refs):
        line = f"{resolved_ref.objid} {resolved_ref.ref}"
        if idx == 0:
            line += '\0' + EXTENSIONS
            for sym_ref in sym_refs:
                line += f" symref={sym_ref.target}:{sym_ref.ref}"
        line += '\n'
        print(encode_pkt_line(line), end='')
    print("0000", end='')

    


if __name__ == '__main__':
    run()