#!/usr/bin/env python3
"""Independent deterministic cumulative zero-B attempts; check is the default.

Only the pinned Python BLAKE3 binding and scalar integer formulas participate.
No native miner/prover/verifier output is used to choose or generate gold data.
The miss-then-found target is deliberately selected from these synthetic hashes;
this fixture selection is not a performance, probability or live-work claim.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import struct

import generate_pearl_fixtures as pearl
import generate_proof_fixtures as proof

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'native/fixtures'
DOMAIN = b'neuro/zero-b/row-seed/v1'
MAX_ATTEMPTS = 64


def row_seed(blake3, root: bytes, index: int) -> bytes:
    if len(root) != 32 or not 0 <= index < MAX_ATTEMPTS:
        raise ValueError('root seed32 and index0..63 required')
    return root if index == 0 else blake3(DOMAIN + root + struct.pack('<I', index)).digest()


def make_case(blake3, index: int, header: bytes) -> dict:
    shape = pearl.Case('attempt', 128, 128, 3)
    signal = bytearray(shape.rows * pearl.K)
    mutations = []
    for attempt in range(index + 1):
        seed = row_seed(blake3, pearl.SEED, attempt)
        folded = pearl.fold_seed(seed)
        row = pearl.splitmix64(folded ^ 0xD1B54A32D192ED03) % shape.rows
        mutations.append(row)
        for k in range(pearl.K):
            position = row * pearl.K + k
            word = pearl.splitmix64(folded ^ ((position * 0x9E3779B97F4A7C15) & pearl.MASK64))
            signal[position] = (((word >> 32) % 128) - 64) & 255
    a = bytes(signal)
    bt = bytes(shape.cols * pearl.K)
    key = blake3(header + pearl.CONFIG).digest()
    ca, cb = blake3(a, key=key).digest(), blake3(bt, key=key).digest()
    salt_a = blake3(b'pearl/cert-v3/noise-seed/A').digest()
    salt_b = blake3(b'pearl/cert-v3/noise-seed/B').digest()
    bound_a = blake3(ca + struct.pack('<I', shape.rows) + bytes(28), key=salt_a).digest()
    bound_b = blake3(cb + struct.pack('<I', shape.cols) + bytes(28), key=salt_b).digest()
    seed_b = blake3(key + bound_b).digest()
    seed_a = blake3(seed_b + bound_a).digest()
    aa, noisy_a = pearl.noisy_matrix(blake3, a, shape.rows, pearl.LABEL_A, seed_a,
                                    pearl.permutation_pairs(blake3, pearl.LABEL_A, seed_a))
    bb, noisy_bt = pearl.noisy_matrix(blake3, bt, shape.cols, pearl.LABEL_B, seed_b,
                                    pearl.permutation_pairs(blake3, pearl.LABEL_B, seed_b))
    transcript, final = pearl.scalar_arithmetic(shape, aa, bb)
    tiles = (shape.rows // pearl.TILE_ROWS) * (shape.cols // pearl.TILE_COLS)
    jackpots = [proof.tile_record(blake3, transcript, seed_a, tiles, shape.cols, t) for t in range(tiles)]
    winner = next(t for t in jackpots if t['meets_fixed_target'])
    return {'index': index, 'replacement_header': header != pearl.HEADER,
            'header_hex': header.hex(), 'root_seed_hex': pearl.SEED.hex(),
            'row_seed_hex': seed.hex(), 'mutation_rows': mutations,
            'digests': {'signal_a': blake3(a).hexdigest(), 'signal_bt': blake3(bt).hexdigest(),
                        'commitment_a': ca.hex(), 'commitment_b': cb.hex(),
                        'a_noise_seed': seed_a.hex(), 'b_noise_seed': seed_b.hex(),
                        'noisy_a': blake3(noisy_a).hexdigest(), 'noisy_bt': blake3(noisy_bt).hexdigest(),
                        'transcript': blake3(struct.pack(f'<{len(transcript)}I', *transcript)).hexdigest(),
                        'final_c': blake3(struct.pack(f'<{len(final)}i', *final)).hexdigest()},
            'first_easy_winner': {'row': winner['row'], 'col': winner['col'], 'hash_hex': winner['hash_hex']},
            'minimum_hash_u256': min(int(t['hash_u256_be_hex'], 16) for t in jackpots),
            'jackpots': jackpots}


def compact_ceiling(value: int) -> tuple[int, int]:
    size = (value.bit_length() + 7) // 8
    shift = max(0, 8 * (size - 3))
    mantissa = (value + (1 << shift) - 1) >> shift
    if mantissa & 0x800000:
        size += 1
        shift += 8
        mantissa = (value + (1 << shift) - 1) >> shift
    return (size << 24) | mantissa, mantissa << shift


def generate():
    blake3 = pearl.load_blake3()
    cases = [make_case(blake3, i, pearl.HEADER) for i in range(4)]
    original = next(c for c in json.loads((DEST/'pearl_manifest.json').read_text())['cases'] if c['name']=='v3_zero_b')
    first = cases[0]
    for name in ['signal_a','signal_bt','noisy_a','noisy_bt','transcript','final_c']:
        if first['digests'][name] != original[name]['blake3']:
            raise ValueError(f'Historical attempt0 differs: {name}')
    # A compact target that is a genuine miss for all preceding attempts and
    # admits a real later tile. Selection uses only independently computed gold.
    selection = None
    for i in range(1, 8):
        if i >= len(cases):
            cases.append(make_case(blake3, i, pearl.HEADER))
        lower = (cases[i]['minimum_hash_u256'] + proof.FACTOR - 1) // proof.FACTOR
        nbits, target = compact_ceiling(lower)
        if all(c['minimum_hash_u256'] > target * proof.FACTOR for c in cases[:i]):
            winner = next(t for t in cases[i]['jackpots'] if int(t['hash_u256_be_hex'], 16) <= target * proof.FACTOR)
            selection = {'winning_attempt': i, 'misses': i, 'nbits': f'0x{nbits:08x}',
                         'target_be_hex': target.to_bytes(32,'big').hex(),
                         'row': winner['row'], 'col': winner['col'], 'hash_hex': winner['hash_hex']}
            break
    if selection is None:
        raise ValueError('No bounded independent miss/continuation fixture; do not change native output to fit')
    rows = []
    collision = None
    for i in range(MAX_ATTEMPTS):
        seed = row_seed(blake3, pearl.SEED, i)
        row = pearl.splitmix64(pearl.fold_seed(seed) ^ 0xD1B54A32D192ED03) % 128
        if row in rows and collision is None:
            collision = i
        rows.append(row)
    if collision is None:
        raise ValueError('Fixed bounded fixture did not contain a row collision')
    for index in (collision, MAX_ATTEMPTS - 1):
        if not any(c['index'] == index for c in cases):
            cases.append(make_case(blake3, index, pearl.HEADER))
    changed = bytearray(pearl.HEADER); changed[-1] ^= 1
    cases.append(make_case(blake3, 0, bytes(changed)))
    digest_names = ['signal_a','signal_bt','commitment_a','commitment_b','a_noise_seed',
                    'b_noise_seed','noisy_a','noisy_bt','transcript','final_c']
    render_digest = lambda text: 'Digest' + pearl.byte_array(bytes.fromhex(text))
    entries=[]
    for c in cases:
        rows='{{'+', '.join(str(x) for x in c['mutation_rows'])+'}}'
        values=',\n        '.join(render_digest(c['digests'][n]) for n in digest_names)
        w=c['first_easy_winner']
        entries.append(f'    {{{c["index"]}, {str(c["replacement_header"]).lower()}, {render_digest(c["row_seed_hex"])}, {rows},\n'
                       f'        {values},\n        {w["row"]}, {w["col"]}, {render_digest(w["hash_hex"])}}},')
    header='\n'.join([
      '// Generated independently by tools/generate_attempt_fixtures.py --write.',
      '// Synthetic cumulative row-mutation fixtures; no native output used.', '#pragma once',
      '#include "neuro/pearl_input.hpp"', '#include <array>',
      'namespace neuro::attempt_golden {', 'using Digest = neuro::pearl::Digest;',
      'struct Fixture { std::uint32_t index; bool replacement_header; Digest row_seed;',
      '  std::array<int,64> mutation_rows;', '  Digest '+', '.join(digest_names)+';',
      '  int winner_row, winner_col; Digest winner_hash; };',
      f'inline const std::array<Fixture,{len(cases)}> fixtures = {{{{', '\n'.join(entries), '}};',
      f'inline constexpr std::uint32_t miss_winning_attempt = {selection["winning_attempt"]};',
      f'inline constexpr Digest miss_target = {pearl.byte_array(bytes.fromhex(selection["target_be_hex"]))};',
      f'inline constexpr int miss_winner_row = {selection["row"]}, miss_winner_col = {selection["col"]};',
      f'inline constexpr Digest miss_winner_hash = {pearl.byte_array(bytes.fromhex(selection["hash_hex"]))};',
      '} // namespace neuro::attempt_golden', '']).encode()
    for c in cases:
        del c['jackpots']; c['minimum_hash_u256']=hex(c['minimum_hash_u256'])
    sources=['tools/generate_attempt_fixtures.py','tools/generate_pearl_fixtures.py',
             'tools/generate_proof_fixtures.py','native/fixtures/pearl_manifest.json','requirements-fixtures.txt']
    manifest={'schema':'neuro.attempt_fixtures.v1', 'seed_domain_ascii':DOMAIN.decode(),
              'counter_encoding':'u32 little endian', 'attempt0':'unchanged root seed', 'maximum_attempts':MAX_ATTEMPTS,
              'mutation_policy':'zero A/B once; overwrite one A row for each index0..i; preserve other rows',
              'root_seed_hex':pearl.SEED.hex(), 'historical_attempt0_exact':True,
              'first_row_collision_attempt':collision,
              'expected_data_source':'independent Python scalar arithmetic and pinned Python BLAKE3',
              'sources_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in sources},
              'header_sha256':hashlib.sha256(header).hexdigest(), 'miss_then_found':selection,
              'gpu_execution':False, 'live_pool':False, 'cases':cases}
    return {'attempt_golden.hpp':header,'attempt_manifest.json':(json.dumps(manifest,indent=2)+'\n').encode()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group();mode.add_argument('--write',action='store_true');mode.add_argument('--check',action='store_true')
    args=parser.parse_args()
    values=generate();mismatches=[]
    for name,data in values.items():
        path=DEST/name
        if args.write:path.write_bytes(data)
        elif not path.exists() or path.read_bytes()!=data:mismatches.append(name)
    print(json.dumps({'schema':'neuro.attempt_fixture_check.v1','passed':not mismatches,
                      'mode':'write' if args.write else 'check','mismatches':mismatches}))
    return bool(mismatches)
if __name__=='__main__':raise SystemExit(main())
