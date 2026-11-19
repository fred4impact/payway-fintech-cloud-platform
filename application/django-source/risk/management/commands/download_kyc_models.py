"""Download the OpenCV face models used by the automated KYC face check.

    python manage.py download_kyc_models [--dest DIR]

Files come from the OpenCV model zoo and are checked against pinned SHA-256 hashes.
The Docker image runs this at build time.
"""
import hashlib
import os
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

BASE = 'https://github.com/opencv/opencv_zoo/raw/main/models/'
MODELS = {
    'face_detection_yunet_2023mar.onnx': (
        BASE + 'face_detection_yunet/face_detection_yunet_2023mar.onnx',
        '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4'),
    'face_recognition_sface_2021dec.onnx': (
        BASE + 'face_recognition_sface/face_recognition_sface_2021dec.onnx',
        '0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79'),
}


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


class Command(BaseCommand):
    help = 'Download the OpenCV face detection and recognition models for KYC checks.'

    def add_arguments(self, parser):
        parser.add_argument('--dest', default=settings.RISK['KYC_MODELS_DIR'])

    def handle(self, *args, dest, **options):
        os.makedirs(dest, exist_ok=True)
        for name, (url, sha) in MODELS.items():
            path = os.path.join(dest, name)
            if os.path.exists(path) and _sha256(path) == sha:
                self.stdout.write(f'{name}: already present')
                continue
            self.stdout.write(f'{name}: downloading')
            tmp = path + '.part'
            # Fixed https URL from MODELS; the file is checksum-verified before use.
            urllib.request.urlretrieve(url, tmp)  # nosec B310
            if _sha256(tmp) != sha:
                os.remove(tmp)
                raise CommandError(f'{name}: checksum mismatch, refusing to install it')
            os.replace(tmp, path)
        self.stdout.write(self.style.SUCCESS(f'KYC face models ready in {dest}'))
