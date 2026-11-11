"""Sample processor: basic image facts. Shows the pipeline end to end."""

import asyncio

from PIL import Image

from processing.base import PhotoTarget, Processor


class PhotoInfoProcessor(Processor):
    name = "photo_info"
    version = "1"
    target = "photo"

    async def run(self, target: PhotoTarget) -> dict:
        return await asyncio.to_thread(self._inspect, target)

    @staticmethod
    def _inspect(target: PhotoTarget) -> dict:
        with Image.open(target.path) as image:
            return {
                "width": image.width,
                "height": image.height,
                "format": image.format,
                "mode": image.mode,
                "has_exif": bool(image.getexif()),
            }
