"""Perceptual fingerprint (dHash) of each photo, for finding reposted ads.

A repost usually re-uploads the same photos, which Yad2 re-encodes, so file hashes differ but
the dHash stays within a few bits.
"""

import asyncio

from PIL import Image

from processing.base import PhotoTarget, Processor


def dhash(image: Image.Image, size: int = 8) -> int:
    gray = image.convert("L").resize((size + 1, size), Image.LANCZOS)
    pixels = list(gray.getdata())
    bits = 0
    for row in range(size):
        for column in range(size):
            left = pixels[row * (size + 1) + column]
            right = pixels[row * (size + 1) + column + 1]
            bits = (bits << 1) | (left > right)
    return bits


def distance(first: int, second: int) -> int:
    return (first ^ second).bit_count()


class PhotoHashProcessor(Processor):
    name = "photo_hash"
    version = "1"
    target = "photo"

    async def run(self, target: PhotoTarget) -> dict:
        return await asyncio.to_thread(self._hash, target)

    @staticmethod
    def _hash(target: PhotoTarget) -> dict:
        with Image.open(target.path) as image:
            return {"dhash": f"{dhash(image):016x}"}
