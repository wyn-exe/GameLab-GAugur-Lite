# formal-v1 dataset card

## Scope

This card describes the historical control dataset produced by the eight bundled Pyxel workloads. It contains 24 solo runs, 480 profile runs, 216 historical colocation runs and 600 target truth rows.

The colocation truth is not a valid reproduction of the paper's effectiveness result: the original run did not carry the required external pressure, and the three subsequent real-workload pilots produced no negative QoS labels.

## Features and splits

The dataset contains target solo FPS, four-resource sensitivity curves, neighbor intensity mean/variance and retention ratio. Splits are combination-level and target_id is excluded from model features. The source truth is retained byte-for-byte under `data/interim/formal-v1/safety-v2/`.

## Limitations

All eight workloads use the same Pyxel engine and are lightweight, frame-capped programs. The dataset therefore supports software-contract and pipeline verification, but not a claim about large commercial cloud games.
