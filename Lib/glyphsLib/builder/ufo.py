# Copyright 2015 Google Inc. All Rights Reserved.
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


from __future__ import (print_function, division, absolute_import,
                        unicode_literals)

from fontTools.misc.py23 import round, unicode

import logging
import re
from collections import deque

from glyphsLib.anchors import propagate_font_anchors
from glyphsLib.util import clear_data, cast_to_number_or_bool, bin_to_int_list
import glyphsLib.glyphdata
from .constants import (
    PUBLIC_PREFIX,
    GLYPHS_PREFIX,
    GLYPHLIB_PREFIX,
    ROBOFONT_PREFIX,
    UFO2FT_FILTERS_KEY,
    GLYPHS_COLORS,
    CODEPAGE_RANGES, )

__all__ = [
    'to_ufos', 'set_custom_params', 'GLYPHS_PREFIX',
]

logger = logging.getLogger(__name__)


def to_ufos(font, include_instances=False, family_name=None, debug=False):
    """Take .glyphs file data and load it into UFOs.

    Takes in data as a dictionary structured according to
    https://github.com/schriftgestalt/GlyphsSDK/blob/master/GlyphsFileFormat.md
    and returns a list of UFOs, one per master.

    If include_instances is True, also returns the parsed instance data.

    If family_name is provided, the master UFOs will be given this name and
    only instances with this name will be returned.

    If debug is True, returns unused input data instead of the resulting UFOs.
    """

    # check that source was generated with at least stable version 2.3
    # https://github.com/googlei18n/glyphsLib/pull/65#issuecomment-237158140
    if font.appVersion < 895:
        logger.warn('This Glyphs source was generated with an outdated version '
                    'of Glyphs. The resulting UFOs may be incorrect.')

    source_family_name = font.familyName
    if family_name is None:
        # use the source family name, and include all the instances
        family_name = source_family_name
        do_filter_instances_by_family = False
    else:
        # use a custom 'family_name' to name master UFOs, and only build
        # instances with matching 'familyName' custom parameter
        do_filter_instances_by_family = True
        if family_name == source_family_name:
            # if the 'family_name' provided is the same as the source, only
            # include instances which do _not_ specify a custom 'familyName'
            instance_family_name = None
        else:
            instance_family_name = family_name

    feature_prefixes, classes, features = [], [], []
    for f in font.featurePrefixes:
        feature_prefixes.append((f.name, f.code, f.automatic))
    for c in font.classes:
        classes.append((c.name, c.code, c.automatic))
    for f in font.features:
        features.append((f.name, f.code, f.automatic, f.disabled, f.notes))
    kerning_groups = {}

    # stores background data from "associated layers"
    supplementary_layer_data = []

    #TODO(jamesgk) maybe create one font at a time to reduce memory usage
    ufos, master_id_order = generate_base_fonts(font, family_name)

    # get the 'glyphOrder' custom parameter as stored in the lib.plist.
    # We assume it's the same for all ufos.
    first_ufo = ufos[master_id_order[0]]
    glyphOrder_key = PUBLIC_PREFIX + 'glyphOrder'
    if glyphOrder_key in first_ufo.lib:
        glyph_order = first_ufo.lib[glyphOrder_key]
    else:
        glyph_order = []
    sorted_glyphset = set(glyph_order)

    for glyph in font.glyphs:
        add_glyph_to_groups(kerning_groups, glyph)
        glyph_name = glyph.name
        if glyph_name not in sorted_glyphset:
            # glyphs not listed in the 'glyphOrder' custom parameter but still
            # in the font are appended after the listed glyphs, in the order
            # in which they appear in the source file
            glyph_order.append(glyph_name)


        for layer in glyph.layers.values():
            layer_id = layer.layerId
            layer_name = layer.name

            assoc_id = layer.associatedMasterId
            if assoc_id != layer.layerId:
                if layer_name is not None:
                    supplementary_layer_data.append(
                        (assoc_id, glyph_name, layer_name, layer))
                continue

            ufo = ufos[layer_id]
            ufo_glyph = ufo.newGlyph(glyph_name)
            load_glyph(ufo_glyph, layer, glyph)

    for layer_id, glyph_name, layer_name, layer_data \
            in supplementary_layer_data:
        ufo_font = ufos[layer_id]
        if layer_name not in ufo_font.layers:
            ufo_layer = ufo_font.newLayer(layer_name)
        else:
            ufo_layer = ufo_font.layers[layer_name]
        ufo_glyph = ufo_layer.newGlyph(glyph_name)
        load_glyph(ufo_glyph, layer_data, layer_data.parent)

    for ufo in ufos.values():
        ufo.lib[glyphOrder_key] = glyph_order
        propagate_font_anchors(ufo)
        add_features_to_ufo(ufo, feature_prefixes, classes, features)
        add_groups_to_ufo(ufo, kerning_groups)

    for master_id, kerning in font.kerning.items():
        load_kerning(ufos[master_id], kerning)

    result = [ufos[master_id] for master_id in master_id_order]

    instances = font.instances
    if do_filter_instances_by_family:
        instances = list(filter_instances_by_family(instances,
                                                    instance_family_name))
    instance_data = {'data': instances}

    # the 'Variation Font Origin' is a font-wide custom parameter, thus it is
    # shared by all the master ufos; here we just get it from the first one
    varfont_origin_key = "Variation Font Origin"
    varfont_origin = first_ufo.lib.get(GLYPHS_PREFIX + varfont_origin_key)
    if varfont_origin:
        instance_data[varfont_origin_key] = varfont_origin
    if debug:
        return clear_data(font)
    elif include_instances:
        return result, instance_data
    return result


def generate_base_fonts(font, family_name):
    """Generate a list of UFOs with metadata loaded from .glyphs data."""
    from defcon import Font

    # "date" can be missing; Glyphs.app removes it on saving if it's empty:
    # https://github.com/googlei18n/glyphsLib/issues/134
    date_created = getattr(font, 'date', None)
    if date_created is not None:
        date_created = to_ufo_time(date_created)
    units_per_em = font.unitsPerEm
    version_major = font.versionMajor
    version_minor = font.versionMinor
    user_data = font.userData
    copyright = font.copyright
    designer = font.designer
    designer_url = font.designerURL
    manufacturer = font.manufacturer
    manufacturer_url = font.manufacturerURL

    misc = ['DisplayStrings', 'disablesAutomaticAlignment', 'disablesNiceNames']
    custom_params = parse_custom_params(font, misc)

    ufos = {}
    master_id_order = []
    for master in font.masters:
        ufo = Font()

        if date_created is not None:
            ufo.info.openTypeHeadCreated = date_created
        ufo.info.unitsPerEm = units_per_em
        ufo.info.versionMajor = version_major
        ufo.info.versionMinor = version_minor

        if copyright:
            ufo.info.copyright = copyright
        if designer:
            ufo.info.openTypeNameDesigner = designer
        if designer_url:
            ufo.info.openTypeNameDesignerURL = designer_url
        if manufacturer:
            ufo.info.openTypeNameManufacturer = manufacturer
        if manufacturer_url:
            ufo.info.openTypeNameManufacturerURL = manufacturer_url

        ufo.info.ascender = master.ascender
        ufo.info.capHeight = master.capHeight
        ufo.info.descender = master.descender
        ufo.info.xHeight = master.xHeight

        horizontal_stems = master.horizontalStems
        vertical_stems = master.verticalStems
        italic_angle = -master.italicAngle
        if horizontal_stems:
            ufo.info.postscriptStemSnapH = horizontal_stems
        if vertical_stems:
            ufo.info.postscriptStemSnapV = vertical_stems
        if italic_angle:
            ufo.info.italicAngle = italic_angle
            is_italic = True
        else:
            is_italic = False

        width = master.width
        weight = master.weight
        custom = master.custom
        if weight:
            ufo.lib[GLYPHS_PREFIX + 'weight'] = weight
        if width:
            ufo.lib[GLYPHS_PREFIX + 'width'] = width
        if custom:
            ufo.lib[GLYPHS_PREFIX + 'custom'] = custom

        styleName = build_style_name(
            width if width != 'Regular' else '',
            weight,
            custom,
            is_italic
        )
        styleMapFamilyName, styleMapStyleName = build_stylemap_names(
            family_name=family_name,
            style_name=styleName,
            is_bold=(styleName == 'Bold'),
            is_italic=is_italic
        )
        ufo.info.familyName = family_name
        ufo.info.styleName = styleName
        ufo.info.styleMapFamilyName = styleMapFamilyName
        ufo.info.styleMapStyleName = styleMapStyleName

        set_blue_values(ufo, master.alignmentZones)
        set_family_user_data(ufo, user_data)
        set_master_user_data(ufo, master.userData)
        set_guidelines(ufo, master)

        set_custom_params(ufo, parsed=custom_params)
        # the misc attributes double as deprecated info attributes!
        # they are Glyphs-related, not OpenType-related, and don't go in info
        misc = ('customValue', 'weightValue', 'widthValue')
        set_custom_params(ufo, data=master, misc_keys=misc, non_info=misc)

        set_default_params(ufo)

        master_id = master.id
        ufo.lib[GLYPHS_PREFIX + 'fontMasterID'] = master_id
        master_id_order.append(master_id)
        ufos[master_id] = ufo

    return ufos, master_id_order


def _get_linked_style(style_name, is_bold, is_italic):
    # strip last occurrence of 'Regular', 'Bold', 'Italic' from style_name
    # depending on the values of is_bold and is_italic
    linked_style = deque()
    is_regular = not (is_bold or is_italic)
    for part in reversed(style_name.split()):
        if part == 'Regular' and is_regular:
            is_regular = False
        elif part == 'Bold' and is_bold:
            is_bold = False
        elif part == 'Italic' and is_italic:
            is_italic = False
        else:
            linked_style.appendleft(part)
    return ' '.join(linked_style)


def build_stylemap_names(family_name, style_name, is_bold=False,
                         is_italic=False, linked_style=None):
    """Build UFO `styleMapFamilyName` and `styleMapStyleName` based on the
    family and style names, and the entries in the "Style Linking" section
    of the "Instances" tab in the "Font Info".

    The value of `styleMapStyleName` can be either "regular", "bold", "italic"
    or "bold italic", depending on the values of `is_bold` and `is_italic`.

    The `styleMapFamilyName` is a combination of the `family_name` and the
    `linked_style`.

    If `linked_style` is unset or set to 'Regular', the linked style is equal
    to the style_name with the last occurrences of the strings 'Regular',
    'Bold' and 'Italic' stripped from it.
    """

    styleMapStyleName = ' '.join(s for s in (
        'bold' if is_bold else '',
        'italic' if is_italic else '') if s) or 'regular'
    if not linked_style or linked_style == 'Regular':
        linked_style = _get_linked_style(style_name, is_bold, is_italic)
    if linked_style:
        styleMapFamilyName = family_name + ' ' + linked_style
    else:
        styleMapFamilyName = family_name
    return styleMapFamilyName, styleMapStyleName


def set_custom_params(ufo, parsed=None, data=None, misc_keys=(), non_info=()):
    """Set Glyphs custom parameters in UFO info or lib, where appropriate.

    Custom parameter data can be pre-parsed out of Glyphs data and provided via
    the `parsed` argument, otherwise `data` should be provided and will be
    parsed. The `parsed` option is provided so that custom params can be popped
    from Glyphs data once and used several times; in general this is used for
    debugging purposes (to detect unused Glyphs data).

    The `non_info` argument can be used to specify potential UFO info attributes
    which should not be put in UFO info.
    """

    if parsed is None:
        parsed = parse_custom_params(data or {}, misc_keys)
    else:
        assert data is None, "Shouldn't provide parsed data and data to parse."

    fsSelection_flags = {'Use Typo Metrics', 'Has WWS Names'}
    for name, value in parsed:
        name = normalize_custom_param_name(name)

        if name in fsSelection_flags:
            if value:
                if ufo.info.openTypeOS2Selection is None:
                    ufo.info.openTypeOS2Selection = []
                if name == 'Use Typo Metrics':
                    ufo.info.openTypeOS2Selection.append(7)
                elif name == 'Has WWS Names':
                    ufo.info.openTypeOS2Selection.append(8)
            continue

        # deal with any Glyphs naming quirks here
        if name == 'disablesNiceNames':
            name = 'useNiceNames'
            value = int(not value)

        # convert code page numbers to OS/2 ulCodePageRange bits
        if name == 'codePageRanges':
            value = [CODEPAGE_RANGES[v] for v in value]

        # convert Glyphs' GASP Table to UFO openTypeGaspRangeRecords
        if name == 'GASP Table':
            name = 'openTypeGaspRangeRecords'
            # XXX maybe the parser should cast the gasp values to int?
            value = {int(k): int(v) for k, v in value.items()}
            gasp_records = []
            # gasp range records must be sorted in ascending rangeMaxPPEM
            for max_ppem, gasp_behavior in sorted(value.items()):
                gasp_records.append({
                    'rangeMaxPPEM': max_ppem,
                    'rangeGaspBehavior': bin_to_int_list(gasp_behavior)})
            value = gasp_records

        opentype_attr_prefix_pairs = (
            ('hhea', 'Hhea'), ('description', 'NameDescription'),
            ('license', 'NameLicense'),
            ('licenseURL', 'NameLicenseURL'),
            ('preferredFamilyName', 'NamePreferredFamilyName'),
            ('preferredSubfamilyName', 'NamePreferredSubfamilyName'),
            ('compatibleFullName', 'NameCompatibleFullName'),
            ('sampleText', 'NameSampleText'),
            ('WWSFamilyName', 'NameWWSFamilyName'),
            ('WWSSubfamilyName', 'NameWWSSubfamilyName'),
            ('panose', 'OS2Panose'),
            ('typo', 'OS2Typo'), ('unicodeRanges', 'OS2UnicodeRanges'),
            ('codePageRanges', 'OS2CodePageRanges'),
            ('weightClass', 'OS2WeightClass'),
            ('widthClass', 'OS2WidthClass'),
            ('win', 'OS2Win'), ('vendorID', 'OS2VendorID'),
            ('versionString', 'NameVersion'), ('fsType', 'OS2Type'))
        for glyphs_prefix, ufo_prefix in opentype_attr_prefix_pairs:
            name = re.sub(
                '^' + glyphs_prefix, 'openType' + ufo_prefix, name)

        postscript_attrs = ('underlinePosition', 'underlineThickness')
        if name in postscript_attrs:
            name = 'postscript' + name[0].upper() + name[1:]

        # enforce that winAscent/Descent are positive, according to UFO spec
        if name.startswith('openTypeOS2Win') and value < 0:
            value = -value

        # The value of these could be a float, and ufoLib/defcon expect an int.
        if name in ('openTypeOS2WeightClass', 'openTypeOS2WidthClass'):
            value = int(value)

        if name == 'glyphOrder':
            # store the public.glyphOrder in lib.plist
            ufo.lib[PUBLIC_PREFIX + name] = value
        elif name == 'Filter':
            filter_struct = parse_glyphs_filter(value)
            if not filter_struct:
                continue
            if UFO2FT_FILTERS_KEY not in ufo.lib.keys():
                ufo.lib[UFO2FT_FILTERS_KEY] = []
            ufo.lib[UFO2FT_FILTERS_KEY].append(filter_struct)
        elif hasattr(ufo.info, name) and name not in non_info:
            # most OpenType table entries go in the info object
            setattr(ufo.info, name, value)
        else:
            # everything else gets dumped in the lib
            ufo.lib[GLYPHS_PREFIX + name] = value


def parse_glyphs_filter(filter_str):
    """Parses glyphs custom filter string into a dict object that
       ufo2ft can consume.

        Reference:
            ufo2ft: https://github.com/googlei18n/ufo2ft
            Glyphs 2.3 Handbook July 2016, p184

        Args:
            filter_str - a string of glyphs app filter

        Return:
            A dictionary contains the structured filter.
            Return None if parse failed.
    """
    elements = filter_str.split(';')

    if elements[0] == '':
        logger.error('Failed to parse glyphs filter, expecting a filter name: \
             %s', filter_str)
        return None

    result = {}
    result['name'] = elements[0]
    for idx, elem in enumerate(elements[1:]):
        if not elem:
            # skip empty arguments
            continue
        if ':' in elem:
            # Key value pair
            key, value = elem.split(':', 1)
            if key.lower() in ['include', 'exclude']:
                if idx != len(elements[1:]) - 1:
                    logger.error('{} can only present as the last argument in the filter. {} is ignored.'.format(key, elem))
                    continue
                result[key.lower()] = re.split('[ ,]+', value)
            else:
                if 'kwargs' not in result:
                    result['kwargs'] = {}
                result['kwargs'][key] = cast_to_number_or_bool(value)
        else:
            if 'args' not in result:
                result['args'] = []
            result['args'].append(cast_to_number_or_bool(elem))
    return result


def set_default_params(ufo):
    """ Set Glyphs.app's default parameters when different from ufo2ft ones.
    """
    # ufo2ft defaults to fsType Bit 2 ("Preview & Print embedding"), while
    # Glyphs.app defaults to Bit 3 ("Editable embedding")
    if ufo.info.openTypeOS2Type is None:
        ufo.info.openTypeOS2Type = [3]

    # Reference:
    # https://glyphsapp.com/content/1-get-started/2-manuals/1-handbook-glyphs-2-0/Glyphs-Handbook-2.3.pdf#page=200
    if ufo.info.postscriptUnderlineThickness is None:
        ufo.info.postscriptUnderlineThickness = 50
    if ufo.info.postscriptUnderlinePosition is None:
        ufo.info.postscriptUnderlinePosition = -100


def normalize_custom_param_name(name):
    """Replace curved quotes with straight quotes in a custom parameter name.
    These should be the only keys with problematic (non-ascii) characters, since
    they can be user-generated.
    """

    replacements = (
        ('\u2018', "'"), ('\u2019', "'"), ('\u201C', '"'), ('\u201D', '"'))
    for orig, replacement in replacements:
        name = name.replace(orig, replacement)
    return name


def set_blue_values(ufo, alignment_zones):
    """Set postscript blue values from Glyphs alignment zones."""

    blue_values = []
    other_blues = []
    for zone in sorted(alignment_zones):
        pos = zone.position
        size = zone.size
        val_list = blue_values if pos == 0 or size >= 0 else other_blues
        val_list.extend(sorted((pos, pos + size)))

    ufo.info.postscriptBlueValues = blue_values
    ufo.info.postscriptOtherBlues = other_blues


def set_guidelines(ufo_obj, glyphs_data):
    """Set guidelines."""
    guidelines = glyphs_data.guideLines
    if not guidelines:
        return
    new_guidelines = []
    for guideline in guidelines:

        x, y = guideline.position
        angle = guideline.angle
        new_guideline = {'x': x, 'y': y, 'angle': (360 - angle) % 360}
        new_guidelines.append(new_guideline)
    ufo_obj.guidelines = new_guidelines


def set_glyph_background(glyph, background):
    """Set glyph background."""

    if not background:
        return

    if glyph.layer.name != 'public.default':
        layer_name = glyph.layer.name + '.background'
    else:
        layer_name = 'public.background'
    font = glyph.font
    if layer_name not in font.layers:
        layer = font.newLayer(layer_name)
    else:
        layer = font.layers[layer_name]
    new_glyph = layer.newGlyph(glyph.name)
    new_glyph.width = glyph.width
    pen = new_glyph.getPointPen()
    draw_paths(pen, background.paths)
    draw_components(pen, background.components)
    add_anchors_to_glyph(new_glyph, background.anchors)
    set_guidelines(new_glyph, background)


def set_family_user_data(ufo, user_data):
    """Set family-wide user data as Glyphs does."""

    for key in user_data.keys():
        ufo.lib[key] = user_data[key]


def set_master_user_data(ufo, user_data):
    """Set master-specific user data as Glyphs does."""

    if user_data:
        data = {}
        for key in user_data.keys():
            data[key] = user_data[key]
        ufo.lib[GLYPHS_PREFIX + 'fontMaster.userData'] = data


def build_style_name(width='', weight='', custom='', is_italic=False):
    """Build style name from width, weight, and custom style strings
    and whether the style is italic.
    """

    return ' '.join(
        s for s in (custom, width, weight, 'Italic' if is_italic else '') if s
    ) or 'Regular'


def to_ufo_time(datetime_obj):
    """Format a datetime object as specified for UFOs."""
    return datetime_obj.strftime('%Y/%m/%d %H:%M:%S')


def parse_custom_params(font, misc_keys):
    """Parse customParameters into a list of <name, val> pairs."""

    params = []
    for p in font.customParameters:
        params.append((p.name, p.value))
    for key in misc_keys:
        try:
            val = getattr(font, key)
        except KeyError:
            continue
        if val is not None:
            params.append((key, val))
    return params


def load_kerning(ufo, kerning_data):
    """Add .glyphs kerning to an UFO."""

    warning_msg = 'Non-existent glyph class %s found in kerning rules.'
    class_glyph_pairs = []

    for left, pairs in kerning_data.items():
        match = re.match(r'@MMK_L_(.+)', left)
        left_is_class = bool(match)
        if left_is_class:
            left = 'public.kern1.%s' % match.group(1)
            if left not in ufo.groups:
                logger.warn(warning_msg % left)
                continue
        for right, kerning_val in pairs.items():
            match = re.match(r'@MMK_R_(.+)', right)
            right_is_class = bool(match)
            if right_is_class:
                right = 'public.kern2.%s' % match.group(1)
                if right not in ufo.groups:
                    logger.warn(warning_msg % right)
                    continue
            if left_is_class != right_is_class:
                if left_is_class:
                    pair = (left, right, True)
                else:
                    pair = (right, left, False)
                class_glyph_pairs.append(pair)
            ufo.kerning[left, right] = kerning_val

    seen = {}
    for classname, glyph, is_left_class in reversed(class_glyph_pairs):
        remove_rule_if_conflict(ufo, seen, classname, glyph, is_left_class)


def remove_rule_if_conflict(ufo, seen, classname, glyph, is_left_class):
    """Check if a class-to-glyph kerning rule has a conflict with any existing
    rule in `seen`, and remove any conflicts if they exist.
    """

    original_pair = (classname, glyph) if is_left_class else (glyph, classname)
    val = ufo.kerning[original_pair]
    rule = original_pair + (val,)

    old_glyphs = ufo.groups[classname]
    new_glyphs = []
    for member in old_glyphs:
        pair = (member, glyph) if is_left_class else (glyph, member)
        existing_rule = seen.get(pair)
        if (existing_rule is not None and
            existing_rule[-1] != val and
            pair not in ufo.kerning):
            logger.warn(
                'Conflicting kerning rules found in %s master for glyph pair '
                '"%s, %s" (%s and %s), removing pair from latter rule' %
                ((ufo.info.styleName,) + pair + (existing_rule, rule)))
        else:
            new_glyphs.append(member)
            seen[pair] = rule

    if new_glyphs != old_glyphs:
        del ufo.kerning[original_pair]
        for member in new_glyphs:
            pair = (member, glyph) if is_left_class else (glyph, member)
            ufo.kerning[pair] = val


def filter_instances_by_family(instances, family_name=None):
    """Yield instances whose 'familyName' custom parameter is
    equal to 'family_name'.
    """
    for instance in instances:
        familyName = None
        for p in instance.customParameters:
            param, value = p.name, p.value
            if param == 'familyName':
                familyName = value
        if familyName == family_name:
            yield instance


def load_glyph_libdata(glyph, layer):
    """Add to a glyph's lib data."""

    set_guidelines(glyph, layer)
    set_glyph_background(glyph, layer.background)
    for key in ['annotations', 'hints']:
        try:
            value = getattr(layer, key)
        except KeyError:
            continue
        if key == 'annotations':
            annotations = []
            for an in list(value.values()):
                annot = {}
                for attr in ['angle', 'position', 'text', 'type', 'width']:
                    val = getattr(an, attr, None)
                    if attr == 'position' and val:
                        val = list(val)
                    if val:
                        annot[attr] = val
                annotations.append(annot)
            value = annotations
        elif key == 'hints':
            hints = []
            for hi in value:
                hint = {}
                for attr in ['horizontal', 'options', 'stem', 'type']:
                    val = getattr(hi, attr, None)
                    hint[attr] = val
                for attr in ['origin', 'other1', 'other2', 'place', 'scale',
                             'target']:
                    val = getattr(hi, attr, None)
                    if val is not None and not any(v is None for v in val):
                        hint[attr] = list(val)
                hints.append(hint)
            value = hints


        if value:
            glyph.lib[GLYPHS_PREFIX + key] = value

    # data related to components stored in lists of booleans
    # each list's elements correspond to the components in order
    for key in ['alignment', 'locked']:
        values = []
        for c in layer.components:
            value = getattr(c, key)
            if value is not None:
                values.append(value)
        if any(values):
            key = key[0].upper() + key[1:]
            glyph.lib['%scomponents%s' % (GLYPHS_PREFIX, key)] = values


def load_glyph(ufo_glyph, layer, glyph_data):
    """Add .glyphs metadata, paths, components, and anchors to a glyph."""

    uval = glyph_data.unicode
    if uval is not None:
        ufo_glyph.unicode = int(uval, 16)
    note = glyph_data.note
    if note is not None:
        ufo_glyph.note = note
    last_change = glyph_data.lastChange
    if last_change is not None:
        ufo_glyph.lib[GLYPHLIB_PREFIX + 'lastChange'] = to_ufo_time(last_change)
    color_index = glyph_data.color
    if color_index is not None and color_index >= 0:
        ufo_glyph.lib[GLYPHLIB_PREFIX + 'ColorIndex'] = color_index
        ufo_glyph.lib[PUBLIC_PREFIX + 'markColor'] = GLYPHS_COLORS[color_index]
    export = glyph_data.export
    if export is not None:
        ufo_glyph.lib[GLYPHLIB_PREFIX + 'Export'] = export
    glyphinfo = glyphsLib.glyphdata.get_glyph(ufo_glyph.name)
    production_name = glyph_data.production or glyphinfo.production_name
    if production_name != ufo_glyph.name:
        postscriptNamesKey = PUBLIC_PREFIX + 'postscriptNames'
        if postscriptNamesKey not in ufo_glyph.font.lib:
            ufo_glyph.font.lib[postscriptNamesKey] = dict()
        ufo_glyph.font.lib[postscriptNamesKey][ufo_glyph.name] = production_name

    for key in ['leftMetricsKey', 'rightMetricsKey', 'widthMetricsKey']:
        glyph_metrics_key = None
        try:
            glyph_metrics_key = getattr(layer, key)
        except KeyError:
            glyph_metrics_key = getattr(glyph_data, key)
        if glyph_metrics_key:
            ufo_glyph.lib[GLYPHLIB_PREFIX + key] = glyph_metrics_key

    # if glyph contains custom 'category' and 'subCategory' overrides, store
    # them in the UFO glyph's lib
    category = glyph_data.category
    if category is None:
        category = glyphinfo.category
    else:
        ufo_glyph.lib[GLYPHLIB_PREFIX + 'category'] = category
    subCategory = glyph_data.subCategory
    if subCategory is None:
        subCategory = glyphinfo.subCategory
    else:
        ufo_glyph.lib[GLYPHLIB_PREFIX + 'subCategory'] = subCategory

    # load width before background, which is loaded with lib data
    width = layer.width
    if width is None:
        pass
    elif category == 'Mark' and subCategory == 'Nonspacing' and width > 0:
        # zero the width of Nonspacing Marks like Glyphs.app does on export
        # TODO: check for customParameter DisableAllAutomaticBehaviour
        ufo_glyph.lib[GLYPHLIB_PREFIX + 'originalWidth'] = width
        ufo_glyph.width = 0
    else:
        ufo_glyph.width = width
    load_glyph_libdata(ufo_glyph, layer)

    pen = ufo_glyph.getPointPen()
    draw_paths(pen, layer.paths)
    draw_components(pen, layer.components)
    add_anchors_to_glyph(ufo_glyph, layer.anchors)


def draw_paths(pen, paths):
    """Draw .glyphs paths onto a pen."""

    for path in paths:
        pen.beginPath()
        nodes = list(path.nodes) # the list is changed below, otherwise you can't draw more than once per session.

        if not nodes:
            pen.endPath()
            continue
        if not path.closed:
            node = nodes.pop(0)
            assert node.type == 'line', 'Open path starts with off-curve points'
            pen.addPoint(tuple(node.position), segmentType='move')
        else:
            # In Glyphs.app, the starting node of a closed contour is always
            # stored at the end of the nodes list.
            nodes.insert(0, nodes.pop())
        for node in nodes:
            node_type = node.type
            if node_type not in ['line', 'curve', 'qcurve']:
                node_type = None
            pen.addPoint(tuple(node.position), segmentType=node_type, smooth=node.smooth)
        pen.endPath()


def draw_components(pen, components):
    """Draw .glyphs components onto a pen, adding them to the parent glyph."""

    for component in components:
        pen.addComponent(component.name,
                         component.transform)


def add_anchors_to_glyph(glyph, anchors):
    """Add .glyphs anchors to a glyph."""

    for anchor in anchors:
        x, y = anchor.position
        anchor_dict = {'name': anchor.name, 'x': x, 'y': y}
        glyph.appendAnchor(glyph.anchorClass(anchorDict=anchor_dict))


def add_glyph_to_groups(kerning_groups, glyph_data):
    """Add a glyph to its kerning groups, creating new groups if necessary."""

    glyph_name = glyph_data.name
    group_keys = {
        '1': 'rightKerningGroup',
        '2': 'leftKerningGroup'}
    for side, group_key in group_keys.items():
        group = getattr(glyph_data, group_key)
        if group is None or len(group) == 0:
            continue
        group = 'public.kern%s.%s' % (side, group)
        kerning_groups[group] = kerning_groups.get(group, []) + [glyph_name]


def add_groups_to_ufo(ufo, kerning_groups):
    """Add kerning groups to an UFO."""

    for name, glyphs in kerning_groups.items():
        ufo.groups[name] = glyphs


def build_gdef(ufo):
    """Build a table GDEF statement for ligature carets."""
    bases, ligatures, marks, carets = set(), set(), set(), {}
    category_key = GLYPHLIB_PREFIX + 'category'
    subCategory_key = GLYPHLIB_PREFIX + 'subCategory'
    for glyph in ufo:
        has_attaching_anchor = False
        for anchor in glyph.anchors:
            name = anchor.name
            if name and not name.startswith('_'):
                has_attaching_anchor = True
            if name and name.startswith('caret_') and 'x' in anchor:
                carets.setdefault(glyph.name, []).append(round(anchor['x']))
        lib = glyph.lib
        glyphinfo = glyphsLib.glyphdata.get_glyph(glyph.name)
        # first check glyph.lib for category/subCategory overrides; else use
        # global values from GlyphData
        category = lib.get(category_key)
        if category is None:
            category = glyphinfo.category
        subCategory = lib.get(subCategory_key)
        if subCategory is None:
            subCategory = glyphinfo.subCategory

        # Glyphs.app assigns glyph classes like this:
        #
        # * Base: any glyph that has an attaching anchor
        #   (such as "top"; "_top" does not count) and is neither
        #   classified as Ligature nor Mark using the definitions below;
        #
        # * Ligature: if subCategory is "Ligature" and the glyph has
        #   at least one attaching anchor;
        #
        # * Mark: if category is "Mark" and subCategory is either
        #   "Nonspacing" or "Spacing Combining";
        #
        # * Compound: never assigned by Glyphs.app.
        #
        # https://github.com/googlei18n/glyphsLib/issues/85
        # https://github.com/googlei18n/glyphsLib/pull/100#issuecomment-275430289
        if subCategory == 'Ligature' and has_attaching_anchor:
            ligatures.add(glyph.name)
        elif category == 'Mark' and (subCategory == 'Nonspacing' or
                                     subCategory == 'Spacing Combining'):
            marks.add(glyph.name)
        elif has_attaching_anchor:
            bases.add(glyph.name)
    if not any((bases, ligatures, marks, carets)):
        return None
    lines = ['table GDEF {', '  # automatic']
    glyphOrder = ufo.lib[PUBLIC_PREFIX + 'glyphOrder']
    glyphIndex = lambda glyph: glyphOrder.index(glyph)
    fmt = lambda g: ('[%s]' % ' '.join(sorted(g, key=glyphIndex))) if g else ''
    lines.extend([
        '  GlyphClassDef',
        '    %s, # Base' % fmt(bases),
        '    %s, # Liga' % fmt(ligatures),
        '    %s, # Mark' % fmt(marks),
        '    ;'])
    for glyph, caretPos in sorted(carets.items()):
        lines.append('  LigatureCaretByPos %s %s;' %
                     (glyph, ' '.join(unicode(p) for p in sorted(caretPos))))
    lines.append('} GDEF;')
    return '\n'.join(lines)


def add_features_to_ufo(ufo, feature_prefixes, classes, features):
    """Write an UFO's OpenType feature file."""

    autostr = lambda automatic: '# automatic\n' if automatic else ''

    prefix_str = '\n\n'.join(
        '# Prefix: %s\n%s%s' % (name, autostr(automatic), code.strip())
        for name, code, automatic in feature_prefixes)

    class_defs = []
    for name, code, automatic in classes:
        if not name.startswith('@'):
            name = '@' + name
        class_defs.append('%s%s = [ %s ];' % (autostr(automatic), name, code))
    class_str = '\n\n'.join(class_defs)

    feature_defs = []
    for name, code, automatic, disabled, notes in features:
        code = code.strip()
        lines = ['feature %s {' % name]
        if notes:
            lines.append('# notes:')
            lines.extend('# ' + line for line in notes.splitlines())
        if automatic:
            lines.append('# automatic')
        if disabled:
            lines.append('# disabled')
            lines.extend('#' + line for line in code.splitlines())
        else:
            lines.append(code)
        lines.append('} %s;' % name)
        feature_defs.append('\n'.join(lines))
    fea_str = '\n\n'.join(feature_defs)
    gdef_str = build_gdef(ufo)

    # make sure feature text is a unicode string, for defcon
    full_text = '\n\n'.join(
        filter(None, [prefix_str, class_str, fea_str, gdef_str])) + '\n'
    ufo.features.text = full_text if full_text.strip() else ''
