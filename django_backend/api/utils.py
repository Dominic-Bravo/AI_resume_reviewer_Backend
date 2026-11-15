import hashlib


def calculate_file_hash(file_obj) -> str:
    sha256_hash = hashlib.sha256()
    file_obj.seek(0)
    for chunk in file_obj.chunks():
        sha256_hash.update(chunk)
    file_obj.seek(0)
    return sha256_hash.hexdigest()
