# Copyright 2016 Intel Corporation
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
# ------------------------------------------------------------------------------

import logging
import hashlib
from threading import RLock

from journal import transaction_block
from journal.messages import transaction_block_message
from journal.consensus.poet1.wait_certificate import WaitCertificate
from journal.consensus.poet1.wait_certificate import WaitTimer
from gossip.common import NullIdentifier

LOGGER = logging.getLogger(__name__)


def register_message_handlers(journal):
    """Registers transaction block message handlers with the journal.

    Args:
        journal (PoetJournal): The journal on which to register the
            message handlers.
    """
    journal.dispatcher.register_message_handler(
        PoetTransactionBlockMessage,
        transaction_block_message.transaction_block_message_handler)


class PoetTransactionBlockMessage(
        transaction_block_message.TransactionBlockMessage):
    """Represents the message format for exchanging information about blocks.

    Attributes:
        PoetTransactionBlockMessage.MessageType (str): The class name of
            the message.
    """
    MessageType = \
        "/journal.consensus.poet1.PoetTransactionBlock/TransactionBlock"

    def __init__(self, minfo=None):
        if minfo is None:
            minfo = {}
        super(PoetTransactionBlockMessage, self).__init__(minfo)

        tinfo = minfo.get('TransactionBlock', {})
        self.TransactionBlock = PoetTransactionBlock(tinfo)


class PoetTransactionBlock(transaction_block.TransactionBlock):
    """A set of transactions to be applied to a ledger, and proof of wait data.

    Attributes:
        PoetTransactionBlock.TransactionBlockTypeName (str): The name of the
            transaction block type.
        PoetTransactionBlock.MessageType (type): The message class.
        PoetTransactionBlock.WaitTimer (wait_timer.WaitTimer): The wait timer
            for the block.
        PoetTransactionBlock.WaitCertificate
            (wait_certificate.WaitCertificate): The wait certificate for the
            block.
    """
    TransactionBlockTypeName = '/Poet/PoetTransactionBlock'
    MessageType = PoetTransactionBlockMessage

    def __init__(self, minfo=None):
        """Constructor for the PoetTransactionBlock class.

        Args:
            minfo (dict): A dict of values for initializing
                PoetTransactionBlocks.
        """
        if minfo is None:
            minfo = {}
        super(PoetTransactionBlock, self).__init__(minfo)

        self._lock = RLock()
        self.WaitTimer = None
        self.WaitCertificate = None

        if 'WaitCertificate' in minfo:
            wc = minfo.get('WaitCertificate')
            serialized_certificate = wc.get('SerializedCertificate')
            signature = wc.get('Signature')
            #
            # To make this work properly we need to be able to take an
            # Originator ID and translate that to the
            # Disguised PoET public key that corresponds to the
            # Originator which was placed in the validator registry.
            #
            poet_public_key = None
            self.WaitCertificate = \
                WaitCertificate.wait_certificate_from_serialized(
                    serialized=serialized_certificate,
                    signature=signature,
                    encoded_poet_public_key=poet_public_key)

        self.AggregateLocalMean = 0.0

    def __getstate__(self):
        state = self.__dict__.copy()
        del state['_lock']
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._lock = RLock()

    def __str__(self):
        return "{0}, {1}, {2}, {3:0.2f}, {4}".format(
            self.BlockNum, self.Identifier[:8], len(self.TransactionIDs),
            self.CommitTime, self.WaitCertificate)

    def __cmp__(self, other):
        """
        Compare two blocks, this will throw an error unless
        both blocks are valid.
        """

        if self.Status != transaction_block.Status.valid:
            raise ValueError('block {0} must be valid for comparison'.format(
                self.Identifier))

        if other.Status != transaction_block.Status.valid:
            raise ValueError('block {0} must be valid for comparison'.format(
                other.Identifier))

        # Criteria #1: if both blocks share the same previous block,
        # then the block with the smallest duration wins
        if self.PreviousBlockID == other.PreviousBlockID:
            if self.WaitCertificate.duration < \
                    other.WaitCertificate.duration:
                return 1
            elif self.WaitCertificate.duration > \
                    other.WaitCertificate.duration:
                return -1
        # Criteria #2: if there is a difference between the immediate
        # ancestors then pick the chain with the highest aggregate
        # local mean, this will be the largest population (more or less)
        else:
            if self.AggregateLocalMean > other.AggregateLocalMean:
                return 1
            elif self.AggregateLocalMean < other.AggregateLocalMean:
                return -1
        # Criteria #3... use number of transactions as a tie breaker, this
        # should not happen except in very rare cases
        return super(PoetTransactionBlock, self).__cmp__(other)

    def update_block_weight(self, journal):
        with self._lock:
            assert self.Status == transaction_block.Status.valid
            super(PoetTransactionBlock, self).update_block_weight(journal)

            assert self.WaitCertificate
            self.AggregateLocalMean = self.WaitCertificate.local_mean

            if self.PreviousBlockID != NullIdentifier:
                assert self.PreviousBlockID in journal.block_store
                self.AggregateLocalMean += \
                    journal.block_store[self.PreviousBlockID]\
                    .AggregateLocalMean

    def is_valid(self, journal):
        """Verifies that the block received is valid.

        This includes checks for valid signature and a valid
        WaitCertificate.

        Args:
            journal (PoetJournal): Journal for pulling context.
        """
        with self._lock:
            if not super(PoetTransactionBlock, self).is_valid(journal):
                return False

            if not self.WaitCertificate:
                LOGGER.info('not a valid block, no wait certificate')
                return False

            #
            # To make this work properly we need to be able to take an
            # Originator ID and translate that to the
            # Disguised PoET public key that corresponds to the
            # Originator which was placed in the validator registry.
            #
            poet_public_key = None

            return \
                self.WaitCertificate.is_valid(
                    certificates=journal.consensus._build_certificate_list(
                        journal.block_store, self),
                    encoded_poet_public_key=poet_public_key)

    def create_wait_timer(self, certlist):
        """Creates a wait timer for the journal based on a list
        of wait certificates.

        Args:
            certlist (list): A list of wait certificates.
        """
        with self._lock:
            self.WaitTimer = WaitTimer.create_wait_timer(certlist)

    def create_wait_certificate(self):
        """Create a wait certificate for the journal based on the wait timer.
        """
        with self._lock:
            LOGGER.debug("WAIT_TIMER: %s", str(self.WaitTimer))
            hasher = hashlib.sha256()
            for tid in self.TransactionIDs:
                hasher.update(tid)
            block_hash = hasher.hexdigest()

            self.WaitCertificate = \
                WaitCertificate.create_wait_certificate(
                    block_digest=block_hash)
            if self.WaitCertificate:
                self.WaitTimer = None

    def wait_timer_has_expired(self, now):
        """Determines if the wait timer has expired.

        Returns:
            bool: Whether or not the wait timer has expired.
        """
        with self._lock:
            return self.WaitTimer.has_expired(now)

    def dump(self):
        """Returns a dict with information about the block.

        Returns:
            dict: A dict containing information about the block.
        """
        with self._lock:
            result = super(PoetTransactionBlock, self).dump()
            result['WaitCertificate'] = self.WaitCertificate.dump()

            return result
