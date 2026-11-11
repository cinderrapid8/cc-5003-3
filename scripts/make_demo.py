#!/usr/bin/env python3
"""Create a demo project with a token-marked synthetic chapter and room tone.

The chapter contains a password marker at the head and tail (four-tone
sequences) plus distinct per-sentence tones, so rendered output can be
analyzed for adoption, length change, and seam placement.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workbench import audio  # noqa: E402
from workbench.store import Store  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--rate", type=int, default=8000)
    ap.add_argument("--sentences", type=int, default=8)
    ap.add_argument("--project", default="演示工程")
    args = ap.parse_args()

    store = Store(args.data)
    project = store.create_project(args.project)
    pcm, manifest = audio.synth_chapter(rate=args.rate, n_sentences=args.sentences)
    src = store.import_recording(project["id"], "source", pcm, args.rate)
    rt = audio.synth_roomtone(args.rate, 6.0)
    rt_rec = store.import_recording(project["id"], "roomtone", rt, args.rate)
    store.set_roomtone(project["id"], rt_rec["id"])
    chapter = store.create_chapter(project["id"], "第一章 · 口令标记", src["id"])

    print(json.dumps({
        "project_id": project["id"],
        "chapter_id": chapter["id"],
        "source_recording_id": src["id"],
        "roomtone_recording_id": rt_rec["id"],
        "rate": args.rate,
        "manifest": manifest,
    }, ensure_ascii=False, indent=2))
    store.close()


if __name__ == "__main__":
    main()
