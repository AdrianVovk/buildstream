#!/usr/bin/env python3
#
#  Copyright (C) 2016 Codethink Limited
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 2 of the License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.	 See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public
#  License along with this library. If not, see <http://www.gnu.org/licenses/>.
#
#  Authors:
#        Tristan Van Berkom <tristan.vanberkom@codethink.co.uk>
#        Jürg Billeter <juerg.billeter@codethink.co.uk>

# BuildStream toplevel imports
from .. import Consistency

# Local imports
from . import Queue, QueueType


# A queue which fetches element sources
#
class FetchQueue(Queue):

    action_name = "Fetch"
    complete_name = "Fetched"
    queue_type = QueueType.FETCH

    def __init__(self, skip_cached=False):
        super().__init__()

        self.skip_cached = skip_cached

    def process(self, element):
        for source in element.sources():
            source._fetch()

    def skip(self, element):
        # Optionally skip elements that are already in the artifact cache
        if self.skip_cached and element._cached():
            return True

        # This will automatically skip elements which
        # have no sources.
        return element._consistency() == Consistency.CACHED

    def done(self, element, result, returncode):

        if returncode != 0:
            return False

        for source in element.sources():

            # Successful fetch, we must be CACHED now
            source._bump_consistency(Consistency.CACHED)

        return True
