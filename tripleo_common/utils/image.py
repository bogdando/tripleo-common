# Copyright 2019 Red Hat, Inc.
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from six.moves.urllib import parse


def uploaded_layers_details(uploaded_layers, layer):
    """Lookup tracked layer in the snapshot of global view, by any scope

    Return the discovered name of the reference image, the known path and kind
    of the layer tracked in global view by a particular kind. A layer may be
    tracked in the local or remote scopes or in both places. For the latter,
    prefer data from the local scope to be returned. The kind of a layer is
    always identified from the known path.
    """
    known_path = None
    known_layer = None
    image = None
    kind = None
    if layer:
        for scp in ['local', 'remote']:
            if scp not in uploaded_layers.keys():
                continue
            known_layer = uploaded_layers[scp].get(layer, None)
            if not known_layer:
                continue
            if known_layer:
                known_path = known_layer.get('path', None)
                image = known_layer.get('ref', None)
                if known_path and parse.urlparse(known_path).netloc:
                    kind = 'remote'
                else:
                    kind = 'local'
                break
    return (known_path, image, kind)


def partial_merge(adict, other, update_bottoms=True, overwrite_node=None):
    """A recursive merge of two graphs (i.e. dictionaries w/o lists in it)

    with alternative update modes for the bottom values. If overwrite_node
    specified, recursive merging stops up to that point and overwrites all
    nodes' data down the road.

    """
    for key, value in other.items():
        if key in adict:
            if (key != overwrite_node and isinstance(adict[key], dict)
               and isinstance(value, dict)):
                partial_merge(adict[key], value, update_bottoms=update_bottoms,
                              overwrite_node=overwrite_node)
            elif update_bottoms or overwrite_node == key:
                adict[key] = value
        else:
            adict[key] = value


def uploaded_layers_targets(uploaded_layers, layer, kind=None):
    """Provide information about images cross-referencing the given layer

    Each uploaded layer has a reference image and its cross-references by other
    images by any scope. The searched targets referencing that reference image
    may be filtered by local or remote kinds, or dumped altogether from any of
    the scopes.

    Returns eigher a list of scope-filtered keys [k, k] or a dict {k:v, k:v}
    containing all items discovered by any scope, whereas the values provide
    scope information for each of the discovered referencing images.
    """
    all_data = {}
    if layer:
        if kind and kind not in ['local', 'remote']:
            return []
        elif kind:
            lookup = [kind]
        else:
            lookup = ['local', 'remote']
        for scope in lookup:
            if scope not in uploaded_layers.keys():
                continue
            known_layer = uploaded_layers[scope].get(layer, None)
            if not known_layer:
                continue
            partial_merge(all_data, known_layer.get('targets', {}))

    if all_data and not kind:
        return all_data
    elif all_data:
        # deduplicate items via list->set->list transition
        return list(set([n for n, s in all_data.items() if s and s == kind]))
    else:
        return []
