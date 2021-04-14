# Copyright (C) 2015-2021 Regents of the University of California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import hashlib
import logging
import reprlib
import uuid
from collections import namedtuple
from contextlib import contextmanager
from typing import Optional
from toil.jobStores.aws.utils import uploadFromPath, copyKeyMultipart
from toil.lib.pipes import ReadablePipe, ReadableTransformingPipe
from toil.lib.checksum import compute_checksum_for_file
from toil.lib.compatibility import compat_bytes
from toil.lib.ec2 import establish_boto3_session
from toil.lib.aws.s3 import MultiPartPipe
from toil.lib.io import AtomicFileCreate

boto3_session = establish_boto3_session()
s3_boto3_resource = boto3_session.resource('s3')
s3_boto3_client = boto3_session.client('s3')
logger = logging.getLogger(__name__)


class ChecksumError(Exception):
    """Raised when a download from AWS does not contain the correct data."""


class AWSFile:
    def __init__(self,
                 fileID,
                 ownerID,
                 encrypted,
                 content=None,
                 numContentChunks=0,
                 checksum=None,
                 sseKeyPath=None):
        """
        :type fileID: str
        :param fileID: the file's ID

        :type ownerID: str
        :param ownerID: ID of the entity owning this file, typically a job ID aka jobStoreID

        :type encrypted: bool
        :param encrypted: whether the file is stored in encrypted form

        :type version: str|None
        :param version: a non-empty string containing the most recent version of the S3
        object storing this file's content, None if the file is new, or empty string if the
        file is inlined.

        :type content: str|None
        :param content: this file's inlined content

        :type numContentChunks: int
        :param numContentChunks: the number of SDB domain attributes occupied by this files

        :type checksum: str|None
        :param checksum: the checksum of the file, if available. Formatted
        as <algorithm>$<lowercase hex hash>.

        inlined content. Note that an inlined empty string still occupies one chunk.
        """
        super(AWSFile, self).__init__()
        self.fileID = fileID
        self.ownerID = ownerID
        self.encrypted = encrypted
        assert content is None or isinstance(content, bytes)
        self._content = content
        self.checksum = checksum
        self._numContentChunks = numContentChunks
        self.sseKeyPath = sseKeyPath

    def upload(self, localFilePath, calculateChecksum=True):
        # actually upload content into s3
        headerArgs = self._s3EncryptionArgs()
        # Create a new Resource in case it needs to be on its own thread
        resource = boto3_session.resource('s3', region_name=self.outer.region)

        self.checksum = compute_checksum_for_file(localFilePath) if calculateChecksum else None
        self.version = uploadFromPath(localFilePath,
                                      resource=resource,
                                      bucketName=self.outer.filesBucket.name,
                                      fileID=compat_bytes(self.fileID),
                                      headerArgs=headerArgs,
                                      partSize=self.outer.partSize)

    @contextmanager
    def uploadStream(self, multipart=True, allowInlining=True):
        """
        Context manager that gives out a binary-mode upload stream to upload data.
        """
        pipe = MultiPartPipe()
        with pipe as writable:
            yield writable

        if not pipe.reader_done:
            raise RuntimeError('Escaped context manager without written data being read!')

    def copyFrom(self, srcObj):
        """
        Copies contents of source key into this file.

        :param S3.Object srcObj: The key (object) that will be copied from
        """
        assert srcObj.content_length is not None
        if srcObj.content_length <= 256:
            self.content = srcObj.get().get('Body').read()
        else:
            # Create a new Resource in case it needs to be on its own thread
            resource = boto3_session.resource('s3', region_name=self.outer.region)
            self.version = copyKeyMultipart(resource,
                                            srcBucketName=compat_bytes(srcObj.bucket_name),
                                            srcKeyName=compat_bytes(srcObj.key),
                                            srcKeyVersion=compat_bytes(srcObj.version_id),
                                            dstBucketName=compat_bytes(self.outer.filesBucket.name),
                                            dstKeyName=compat_bytes(self._fileID),
                                            sseAlgorithm='AES256',
                                            sseKey=self._getSSEKey())

    def copyTo(self, dstObj):
        """
        Copies contents of this file to the given key.

        :param S3.Object dstObj: The key (object) to copy this file's content to
        """
        if self.content is not None:
            dstObj.put(Body=self.content)
        elif self.version:
            # Create a new Resource in case it needs to be on its own thread
            resource = boto3_session.resource('s3', region_name=self.outer.region)

            copyKeyMultipart(resource,
                             srcBucketName=compat_bytes(self.outer.filesBucket.name),
                             srcKeyName=compat_bytes(self.fileID),
                             srcKeyVersion=compat_bytes(self.version),
                             dstBucketName=compat_bytes(dstObj.bucket_name),
                             dstKeyName=compat_bytes(dstObj.key),
                             copySourceSseAlgorithm='AES256',
                             copySourceSseKey=self._getSSEKey())
        else:
            assert False

    def download(self, localFilePath, verifyChecksum=True):
        if self.content is not None:
            with AtomicFileCreate(localFilePath) as tmpPath:
                with open(tmpPath, 'wb') as f:
                    f.write(self.content)
        elif self.version:
            headerArgs = self._s3EncryptionArgs()
            obj = self.outer.filesBucket.Object(compat_bytes(self.fileID))

            with AtomicFileCreate(localFilePath) as tmpPath:
                obj.download_file(Filename=tmpPath, ExtraArgs={'VersionId': self.version, **headerArgs})

            if verifyChecksum and self.checksum:
                algorithm, expected_checksum = self.checksum.split('$')
                computed = compute_checksum_for_file(localFilePath, algorithm=algorithm)
                if self.checksum != computed:
                    raise ChecksumError(f'Checksum mismatch for file {localFilePath}. '
                                        f'Expected: {self.checksum} Actual: {computed}')
                # The error will get caught and result in a retry of the download until we run out of retries.
                # TODO: handle obviously truncated downloads by resuming instead.
        else:
            assert False

    @contextmanager
    def downloadStream(self, verifyChecksum=True):
        info = self

        class DownloadPipe(ReadablePipe):
            def writeTo(self, writable):
                if info.content is not None:
                    writable.write(info.content)
                elif info.version:
                    headerArgs = info._s3EncryptionArgs()
                    obj = info.outer.filesBucket.Object(compat_bytes(info.fileID))
                    obj.download_fileobj(writable, ExtraArgs={'VersionId': info.version, **headerArgs})
                else:
                    assert False

        class HashingPipe(ReadableTransformingPipe):
            """
            Class which checksums all the data read through it. If it
            reaches EOF and the checksum isn't correct, raises
            ChecksumError.

            Assumes info actually has a checksum.
            """

            def transform(self, readable, writable):
                algorithm, _ = info.checksum.split('$')
                hasher = getattr(hashlib, algorithm)()
                contents = readable.read(1024 * 1024)
                while contents != b'':
                    hasher.update(contents)
                    try:
                        writable.write(contents)
                    except BrokenPipeError:
                        # Read was stopped early by user code.
                        # Can't check the checksum.
                        return
                    contents = readable.read(1024 * 1024)
                # We reached EOF in the input.
                # Finish checksumming and verify.
                result_hash = hasher.hexdigest()
                if f'{algorithm}${result_hash}' != info.checksum:
                    raise ChecksumError('')
                # Now stop so EOF happens in the output.

        with DownloadPipe() as readable:
            if verifyChecksum and self.checksum:
                # Interpose a pipe to check the hash
                with HashingPipe(readable) as verified:
                    yield verified
            else:
                # No true checksum available, so don't hash
                yield readable

    def delete(self):
        store = self.outer
        if self.previousVersion is not None:
            store.filesDomain.delete_attributes(
                compat_bytes(self.fileID),
                expected_values=['version', self.previousVersion])
            if self.previousVersion:
                store.s3_client.delete_object(Bucket=store.filesBucket.name,
                                              Key=compat_bytes(self.fileID),
                                              VersionId=self.previousVersion)

    def getSize(self):
        """
        Return the size of the referenced item in bytes.
        """
        if self.content is not None:
            return len(self.content)
        elif self.version:
            obj = self.outer.filesBucket.Object(compat_bytes(self.fileID))
            return obj.content_length
        else:
            return 0