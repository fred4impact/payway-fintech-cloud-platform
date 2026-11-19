"""Automated KYC checks: read the ID document and compare the face on it with the selfie.

* Document: Tesseract OCR reads the ID; we look for the applicant's name and date of birth
  in the text, guess the document type, and flag blurry images.
* Face: OpenCV's YuNet detector finds the faces and the SFace model turns each into an
  embedding; cosine similarity says whether the ID photo and the selfie are the same person.

Everything runs locally; no image leaves the server. The result fills the KYC record's
existing document_verified / face_verified / verification_confidence / ai_* fields and is
queued for a person to approve or reject. The checks never approve or reject on their own.

Each check degrades on its own: if Tesseract or the face models are missing, that part is
reported as unavailable and the rest still runs.
"""
import difflib
import logging
import os
import re
import shutil
import unicodedata
from datetime import date

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

MODEL_VERSION = 'kyc-v1'
DETECTOR_FILE = 'face_detection_yunet_2023mar.onnx'
RECOGNIZER_FILE = 'face_recognition_sface_2021dec.onnx'
BLUR_THRESHOLD = 60.0          # variance of the Laplacian; lower is blurrier
MAX_SIDE = 1600                # downscale big phone photos before processing

DOC_TYPES = [
    ('passport', ('passport', 'passeport', 'pasaporte', 'reisepass')),
    ('driving_licence', ('driving licence', 'driving license', "driver's license", 'drivers license', 'driver license', 'permis de conduire')),
    ('national_id', ('identity card', 'national id', 'id card', 'carte nationale', "carte d'identit", 'personalausweis', 'documento nacional')),
    ('residence_permit', ('residence permit', 'permanent resident', 'titre de s')),
]


def _cfg(key, default=None):
    return getattr(settings, 'RISK', {}).get(key, default)


# ---------------------------------------------------------------- helpers

def _normalise(text):
    """Upper-case ASCII letters, digits and single spaces: 'José  O'Neil' -> 'JOSE O NEIL'."""
    text = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode()
    return re.sub(r'[^A-Z0-9]+', ' ', text.upper()).strip()


def name_match(full_name, text):
    """Share (0-1) of the applicant's name parts found in the OCR text, allowing OCR slips."""
    parts = [p for p in _normalise(full_name).split() if len(p) >= 2]
    words = set(_normalise(text).split())
    if not parts or not words:
        return 0.0
    found = sum(1 for p in parts if p in words or difflib.get_close_matches(p, words, n=1, cutoff=0.8))
    return found / len(parts)


def dob_found(dob, text):
    """Whether the date of birth appears in the text in any common format."""
    if not isinstance(dob, date):
        return False
    flat = _normalise(text)
    compact = flat.replace(' ', '')
    variants = {
        dob.strftime('%d %m %Y'), dob.strftime('%m %d %Y'), dob.strftime('%Y %m %d'),
        dob.strftime('%d %b %Y').upper(), dob.strftime('%d %B %Y').upper(), dob.strftime('%b %d %Y').upper(),
        f'{dob.day} {dob.strftime("%b").upper()} {dob.year}', f'{dob.day} {dob.strftime("%B").upper()} {dob.year}',
        dob.strftime('%d %m %y'), dob.strftime('%y%m%d'),      # short year, and the passport MRZ format
    }
    return any(v in flat or v.replace(' ', '') in compact for v in variants)


def classify_document(text):
    lower = (text or '').lower()
    for doc_type, keywords in DOC_TYPES:
        if any(k in lower for k in keywords):
            return doc_type
    if re.search(r'P<[A-Z]{3}', text or ''):      # passport machine-readable zone
        return 'passport'
    return 'unknown'


def _load_image(field):
    """Read an ImageField into an OpenCV BGR array, downscaled to MAX_SIDE."""
    import cv2
    import numpy as np
    field.open('rb')
    try:
        data = np.frombuffer(field.read(), dtype=np.uint8)
    finally:
        field.close()
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError('not a readable image')
    h, w = img.shape[:2]
    scale = MAX_SIDE / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return img


def sharpness(img):
    import cv2
    return float(cv2.Laplacian(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())


# ---------------------------------------------------------------- checks

def tesseract_available():
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return shutil.which(os.environ.get('TESSERACT_CMD', 'tesseract')) is not None


def check_document(kyc, img):
    report = {'available': tesseract_available()}
    report['sharpness'] = round(sharpness(img), 1)
    report['blurry'] = report['sharpness'] < BLUR_THRESHOLD
    if not report['available']:
        report['note'] = 'Tesseract OCR is not installed'
        return report

    import cv2
    import pytesseract
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if max(gray.shape) < 1000:  # OCR reads small text better after upscaling
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    gray = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15)
    text = pytesseract.image_to_string(gray, timeout=30)

    report.update(
        text_chars=len(text.strip()),
        name_match=round(name_match(kyc.full_name, text), 2),
        dob_found=dob_found(kyc.date_of_birth, text),
        document_type=classify_document(text),
    )
    report['verified'] = report['name_match'] >= 0.66 and report['dob_found'] and not report['blurry']
    return report


_face_models = None


def _models():
    global _face_models
    if _face_models is None:
        import cv2
        root = _cfg('KYC_MODELS_DIR', '')
        det, rec = os.path.join(root, DETECTOR_FILE), os.path.join(root, RECOGNIZER_FILE)
        if not (os.path.exists(det) and os.path.exists(rec)):
            return None
        _face_models = (cv2.FaceDetectorYN.create(det, '', (320, 320), 0.8), cv2.FaceRecognizerSF.create(rec, ''))
    return _face_models


def _largest_face(detector, img):
    h, w = img.shape[:2]
    detector.setInputSize((w, h))
    _, faces = detector.detect(img)
    if faces is None or len(faces) == 0:
        return None, 0
    return max(faces, key=lambda f: f[2] * f[3]), len(faces)


def check_face(id_img, selfie_img):
    try:
        import cv2  # noqa: F401
        models = _models()
    except ImportError:
        models = None
    if models is None:
        return {'available': False, 'note': 'Face models are not installed (run manage.py download_kyc_models)'}

    import cv2
    detector, recognizer = models
    id_face, id_count = _largest_face(detector, id_img)
    selfie_face, selfie_count = _largest_face(detector, selfie_img)
    report = {'available': True, 'faces_on_id': id_count, 'faces_on_selfie': selfie_count}
    if id_face is None or selfie_face is None:
        report['verified'] = False
        report['note'] = 'No face found on the ' + ('ID document' if id_face is None else 'selfie')
        return report

    feats = [recognizer.feature(recognizer.alignCrop(img, face)) for img, face in ((id_img, id_face), (selfie_img, selfie_face))]
    similarity = float(recognizer.match(feats[0], feats[1], cv2.FaceRecognizerSF_FR_COSINE))
    report['similarity'] = round(similarity, 3)
    report['verified'] = similarity >= _cfg('KYC_FACE_MATCH_THRESHOLD', 0.363)
    return report


# ---------------------------------------------------------------- orchestration

def _confidence(doc, face):
    """0-100 confidence over the checks that could run."""
    parts = []
    if doc.get('available'):
        parts.append((0.6 * doc.get('name_match', 0) + 0.4 * (1 if doc.get('dob_found') else 0))
                     * (0.6 if doc.get('blurry') else 1))
    if face.get('available'):
        sim = face.get('similarity')
        threshold = _cfg('KYC_FACE_MATCH_THRESHOLD', 0.363)
        # map similarity onto 0..1 with the match threshold landing at 0.7
        parts.append(0.0 if sim is None else max(0.0, min(1.0, 0.7 * sim / threshold if sim < threshold
                                                             else 0.7 + 0.3 * (sim - threshold) / (1 - threshold))))
    return round(100 * sum(parts) / len(parts), 1) if parts else 0.0


def run_checks(kyc):
    """Run every available check on a KYC submission, save the results, queue it for review."""
    from .models import RiskDecision

    reasons, doc, face = [], {'available': False}, {'available': False}
    try:
        id_img = _load_image(kyc.id_document)
        selfie_img = _load_image(kyc.selfie_photo)
    except Exception as e:  # unreadable upload, missing file, OpenCV not installed
        logger.warning('KYC %s: could not read images: %s', kyc.pk, e)
        reasons.append(f'Could not read the uploaded images ({e.__class__.__name__})')
        id_img = selfie_img = None

    if id_img is not None:
        try:
            doc = check_document(kyc, id_img)
        except Exception as e:
            logger.exception('KYC %s: document check failed', kyc.pk)
            doc = {'available': False, 'note': f'Document check failed: {e.__class__.__name__}'}
        try:
            face = check_face(id_img, selfie_img)
        except Exception as e:
            logger.exception('KYC %s: face check failed', kyc.pk)
            face = {'available': False, 'note': f'Face check failed: {e.__class__.__name__}'}

    if doc.get('available'):
        if doc.get('name_match', 0) < 0.66:
            reasons.append(f"Name on the document doesn't match the application ({doc.get('name_match', 0):.0%} of name parts found)")
        if not doc.get('dob_found'):
            reasons.append('Date of birth not found on the document')
        if doc.get('document_type') == 'unknown':
            reasons.append('Document type not recognised')
    if doc.get('blurry'):
        reasons.append('ID image is blurry')
    if face.get('available'):
        if 'similarity' in face:
            reasons.append(f"Face similarity {face['similarity']:.2f} "
                           f"({'match' if face['verified'] else 'no match'} at threshold {_cfg('KYC_FACE_MATCH_THRESHOLD', 0.363)})")
        elif face.get('note'):
            reasons.append(face['note'])
    for part in (doc, face):
        if not part.get('available') and part.get('note'):
            reasons.append(part['note'])

    ran = [p for p in (doc, face) if p.get('available')]
    if not ran:
        status = 'unavailable'
    elif len(ran) == 2 and all(p.get('verified') for p in ran):
        status = 'passed'
    elif any(p.get('verified') for p in ran):
        status = 'review'          # one check passed, or only one could run
    else:
        status = 'failed'

    confidence = _confidence(doc, face)
    face_mismatch = face.get('available') and 'similarity' in face and face['similarity'] < 0.2
    name_mismatch = doc.get('available') and doc.get('text_chars', 0) > 20 and doc.get('name_match', 1) < 0.34
    kyc.document_verified = bool(doc.get('verified'))
    kyc.face_verified = bool(face.get('verified'))
    kyc.verification_confidence = confidence
    kyc.ai_verification_status = status
    kyc.ai_flagged = bool(face_mismatch or name_mismatch)
    kyc.risk_score = round(1 - confidence / 100, 2) if ran else 0.0
    kyc.ai_report = {'document': doc, 'face': face, 'reasons': reasons, 'model_version': MODEL_VERSION}
    kyc.ai_checked_at = timezone.now()
    kyc.save(update_fields=['document_verified', 'face_verified', 'verification_confidence', 'ai_verification_status',
                            'ai_flagged', 'risk_score', 'ai_report', 'ai_checked_at', 'updated_at'])

    # One open review item per submission: replace an earlier one if the checks are re-run.
    RiskDecision.objects.filter(kind='kyc', result_ref=str(kyc.pk), review_status='pending').delete()
    RiskDecision.objects.create(
        user=kyc.user, kind='kyc', subject='kyc', score=int(round(100 - confidence)) if ran else 50,
        action='review', reasons=reasons, features={'document': doc, 'face': face, 'status': status},
        model_version=MODEL_VERSION, review_status='pending' if kyc.verification_status == 'pending' else 'none',
        result_ref=str(kyc.pk),
    )
    return kyc.ai_report


def enqueue(kyc_id):
    """Run the checks on the Celery worker; run them here if the broker can't be reached."""
    from account.models import KYC
    from .tasks import run_kyc_checks
    if _cfg('KYC_ASYNC', True):
        try:
            run_kyc_checks.apply_async(args=[kyc_id], retry=False)
            return
        except Exception as e:
            logger.warning('KYC %s: Celery unavailable (%s); running checks in-process', kyc_id, e)
    run_checks(KYC.objects.get(pk=kyc_id))
