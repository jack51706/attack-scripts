import argparse
from stix2 import MemoryStore, Filter, TAXIICollectionSource
from taxii2client import Collection
import os
import json
from tqdm import tqdm
import datetime
from string import Template
from itertools import chain
from dateutil import parser as dateparser

# helper maps
domainToDomainLabel = {
    'enterprise-attack': 'Enterprise', 
    'pre-attack': 'PRE-ATT&CK', 
    'mobile-attack': 'Mobile'
}
domainToLayerFileDomain = {
    'enterprise-attack': 'mitre-enterprise',
    'mobile-attack': 'mitre-mobile',
    'pre-attack': 'pre-attack'
}
domainToTaxiiCollectionId = {
    "enterprise-attack": "95ecc380-afe9-11e4-9b6c-751b66dd541e",
    "mobile-attack": "2f669986-b40b-4423-b720-4396ca6a462b",
    "pre-attack": "062767bd-02d2-4b72-84ba-56caef0f8658"
}
attackTypeToStixFilter = { # stix filters for querying for each type of data
    'technique': [Filter('type', '=', 'attack-pattern')],
    'software': [Filter('type', '=', 'malware'), Filter('type', '=', 'tool')],
    'group': [Filter('type', '=', 'intrusion-set')],
    'mitigation': [Filter('type', '=', 'course-of-action')]
}
attackTypeToPlural = { # because some of these pluralize differently
    'technique': 'techniques',
    'malware': 'malware',
    'software': 'software',
    'group': 'groups',
    'mitigation': 'mitigations'
}
sectionNameToSectionHeaders = { # how we want to format headers for each section
    "additions": "New {obj_type}",
    "changes": "{obj_type} changes",
    "minor_changes": "Minor {obj_type} changes",
    "deprecations": "{obj_type} deprecations",
    "revocations": "{obj_type} revocations",
    "deletions": "{obj_type} deletions"
}
statusToColor = { # color key for layers
    "additions": "#a1d99b",
    "changes": "#fcf3a2",
    "minor_changes": "#c7c4e0",
    "deletions": "#ff00e1", # this will probably never show up but just in case
    "revocations": "#ff9000",
    "deprecations": "#ff6363"
}
statusDescriptions = { # explanation of modification types to data objects for legend in layer files
    "additions": "objects which are present in the new data and not the old",
    "changes": "objects which have a newer version number in the new data compared to the old",
    "minor_changes": "objects which have a newer last edit date in the new data than in the old, but the same version number",
    "revocations": "objects which are revoked in the new data but not in the old",
    "deprecations": "objects which are deprecated in the new data but not in the old",
    "deletions": "objects which are present in the old data but not the new"
}

class DiffStix(object):
    """
    Utilities for detecting and summarizing differences between two versions of the ATT&CK content.
    """
    def __init__(
        self,
        domains=['enterprise-attack', 'pre-attack', 'mobile-attack'],
        layers=None,
        markdown=None,
        minor_changes=False,
        new='new',
        old='old',
        show_key=False,
        site_prefix='',
        types=['technique', 'software', 'group', 'mitigation'],
        use_taxii=False,
        verbose=False
    ):
        """
        Construct a new 'DiffStix' object.

        params:
            domains: list of domains to parse, e.g. enterprise-attack, mobile-attack
            layers: array of output filenames for layer files, e.g. ['enterprise.json', 'mobile.json', 'pre.json']
            markdown: output filename for markdown content to be written to
            minor_changes: if true, also report minor changes section (changes which didn't increment version)
            new: directory to load for new stix version
            old: directory to load for old stix version
            show_key: if true, output key to markdown file
            site_prefix: prefix links in markdown output
            types: which types of objects to report on, e.g technique, software
            verbose: if true, print progress bar and status messages to stdout
        """
        self.domains = domains
        self.layers = layers
        self.markdown = markdown
        self.minor_changes = minor_changes
        self.new = new
        self.old = old
        self.show_key = show_key
        self.site_prefix = site_prefix
        self.types = types
        self.use_taxii = use_taxii
        self.verbose = verbose

        self.data = {   # data gets load into here in the load() function. All other functionalities rely on this data structure
            # technique {
                # enterprise-attack {
                    # additions: [],
                    # deletions: [],
                    # changes: [],
                    # minor_changes: [],
                    # revocations: [],
                    # deprecations: []
                # }
                # mobile-attack...
            # }
            # software...
        }
        self.load_data()


    def verboseprint(self, *args, **kwargs):
        if self.verbose:
            print(*args, **kwargs)


    def getUrlFromStix(self, datum):
        """
        Parse the website url from a stix object.
        """
        url = datum['external_references'][0]['url']
        split_url = url.split('/')
        link = '/'.join(split_url[-2:])
        return link


    def deep_copy_stix(self, objects):
        """
        Transform stix to dict and deep copy the dict.
        """
        result = []
        for obj in objects:
            obj = dict(obj)
            if 'external_references' in obj:
                for i in range(len(obj['external_references'])):
                    obj['external_references'][i] = dict(
                        obj['external_references'][i])
            if 'kill_chain_phases' in obj:
                for i in range(len(obj['kill_chain_phases'])):
                    obj['kill_chain_phases'][i] = dict(obj['kill_chain_phases'][i])
            if 'modified' in obj:
                obj['modified'] = str(obj['modified'])
            if 'definition' in obj:
                obj['definition'] = dict(obj['definition'])
            obj['created'] = str(obj['created'])
            result.append(obj)
        return result


    # load data into data structure
    def load_data(self):
        """
        Load data from files into data dict.
        """
        if self.verbose:
            pbar = tqdm(total=len(self.types) * len(self.domains), desc="loading data", bar_format="{l_bar}{bar}| [{elapsed}<{remaining}, {rate_fmt}{postfix}]")
        for obj_type in self.types:
            for domain in self.domains:
                # handle data loaded from either a directory or the TAXII server
                def load_datastore(data_store):
                    raw_data = list(chain.from_iterable(
                        data_store.query(f) for f in attackTypeToStixFilter[obj_type]
                    ))
                    raw_data = self.deep_copy_stix(raw_data)
                    id_to_obj = {item['id']: item for item in raw_data}

                    return {
                        "id_to_obj": id_to_obj,
                        "keys": set(id_to_obj.keys()),
                        "data_store": data_store
                    }

                # load data from directory according to domain
                def load_dir(dir):
                    data_store = MemoryStore()
                    datafile = os.path.join(dir, domain + ".json")
                    data_store.load_from_file(datafile)

                    return load_datastore(data_store)

                # load data from TAXII server according to domain
                def load_taxii():
                    collection = Collection("https://cti-taxii.mitre.org/stix/collections/" + domainToTaxiiCollectionId[domain])
                    data_store = TAXIICollectionSource(collection)

                    return load_datastore(data_store)

                if self.use_taxii:
                    old = load_taxii()
                else:
                    old = load_dir(self.old)
                new = load_dir(self.new)

                intersection = old["keys"] & new["keys"]
                additions = new["keys"] - old["keys"]
                deletions = old["keys"] - new["keys"]

                # sets to store the ids of objects for each section
                changes = set()
                minor_changes = set()
                revocations = set()
                deprecations = set()

                # find changes, revocations and deprecations
                for key in intersection:
                    if "revoked" in new["id_to_obj"][key] and new["id_to_obj"][key]["revoked"]:
                        if not "revoked" in old["id_to_obj"][key] or not old["id_to_obj"][key]["revoked"]: # if it was previously revoked, it's not a change
                            # store the revoking object
                            revoked_by_key = new["data_store"].query([
                                Filter('type', '=', 'relationship'),
                                Filter('relationship_type', '=', 'revoked-by'),
                                Filter('source_ref', '=', key)
                            ])[0]["target_ref"]
                            new["id_to_obj"][key]["revoked_by"] = new["id_to_obj"][revoked_by_key]

                            revocations.add(key)
                        # else it was already revoked, and not a change; do nothing with it
                    elif "x_mitre_deprecated" in new["id_to_obj"][key] and new["id_to_obj"][key]["x_mitre_deprecated"]:
                        if not "x_mitre_deprecated" in old["id_to_obj"][key]:   # if previously deprecated, not a change
                            deprecations.add(key)
                    else: # not revoked or deprecated
                        # try getting version numbers; should only lack version numbers if something has gone
                        # horribly wrong or a revoked object has slipped through
                        try:
                            old_version = float(old["id_to_obj"][key]["x_mitre_version"])
                        except: 
                            print("old\n\t" +key)
                        try:
                            new_version = float(new["id_to_obj"][key]["x_mitre_version"])
                        except: 
                            print("new\n\t" + key)

                        # check for changes
                        if new_version > old_version:
                            # an update has occurred to this object
                            changes.add(key)
                        else:
                            # check for minor change; modification date increased but not version
                            old_date = dateparser.parse(old["id_to_obj"][key]["modified"])
                            new_date = dateparser.parse(new["id_to_obj"][key]["modified"])
                            if new_date > old_date:
                                minor_changes.add(key)
                
                # set data
                if obj_type not in self.data: self.data[obj_type] = {}
                self.data[obj_type][domain] = {
                    "additions":     [new["id_to_obj"][key] for key in additions],
                    "changes":       [new["id_to_obj"][key] for key in changes]
                }
                # only create minor_changes data if we want to display it later
                if self.minor_changes:
                    self.data[obj_type][domain]["minor_changes"] = [new["id_to_obj"][key] for key in minor_changes]
                self.data[obj_type][domain]["revocations"] = [new["id_to_obj"][key] for key in revocations]
                self.data[obj_type][domain]["deprecations"] = [new["id_to_obj"][key] for key in deprecations]
                # only show deletions if objects were deleted
                if len(deletions) > 0:
                    self.data[obj_type][domain]["deletions"] = [old["id_to_obj"][key] for key in deletions]
                if self.verbose:
                    pbar.update(1)
        if self.verbose:
            pbar.close()


    def get_md_key(self):
        """
        Create string describing each type of difference (change, addition, etc). Used in get_markdown_string.

        Includes minor changes if the DiffStix instance was instantiated with the minor_changes argument.

        Includes deletions if the changes include deletions.
        """

        have_deletions = False
        for types in self.data.keys():
            for domain in self.data[types].keys():
                if "deletions" in self.data[types][domain].keys():
                    have_deletions = True

        key = "#### Key\n\n"
        key += (
            "* New objects: " + statusDescriptions['additions'] + "\n"
            "* Object changes: " + statusDescriptions['changes'] + "\n"
        )
        if self.minor_changes:
            key += "* Minor object changes: " + statusDescriptions['minor_changes'] + "\n"
        key += (
            "* Object revocations: " + statusDescriptions['revocations'] + "\n"
            "* Object deprecations: " + statusDescriptions['deprecations']
        )
        if have_deletions:
            key += "\n" + "* Object deletions: " + statusDescriptions['deletions']
        return f"{key}"


    def get_markdown_string(self):
        """
        Return a markdown string summarizing detected differences.
        """
        
        self.verboseprint("generating markdown string... ", end="", flush="true")

        content = ""
        for obj_type in self.data.keys():
            domains = ""
            for domain in self.data[obj_type]:
                domain_sections = ""
                for section in self.data[obj_type][domain]:
                    if section == "revocations":
                        # handle revoked by
                        section_items = list(map(lambda d: f"* {d['name']} (revoked by [{d['revoked_by']['name']}]({self.site_prefix}/{self.getUrlFromStix(d['revoked_by'])}))", self.data[obj_type][domain][section]))
                    elif section == "deletions": 
                        section_items = list(map(lambda d: f"* {d['name']}", self.data[obj_type][domain][section]))
                    else:
                        section_items = list(map(lambda d: f"* [{d['name']}]({self.site_prefix}/{self.getUrlFromStix(d)})", self.data[obj_type][domain][section]))
                    
                    if len(section_items) > 0:
                        section_items = "\n".join(sorted(section_items))
                    else:
                        section_items = "No changes"
                    header = sectionNameToSectionHeaders[section] + ":"
                    if "{obj_type}" in header:
                        if section == "additions":
                            header = header.replace("{obj_type}", attackTypeToPlural[obj_type].capitalize())
                        else: header = header.replace("{obj_type}", obj_type.capitalize())
                    if section_items == "No changes":
                        domain_sections += f"{header}\n{section_items}\n\n" # e.g "added techniques:"
                    else: domain_sections += f"{header}\n\n{section_items}\n\n" # add empty line between header and section list
                domains += f"**{domainToDomainLabel[domain]}**\n\n{domain_sections}" # e.g "enterprise"
            content += f"### {attackTypeToPlural[obj_type].capitalize()}\n\n{domains}" # e.g "techniques"

        if self.show_key:
            key_content = self.get_md_key()
            content = f"{key_content}\n\n{content}"

        self.verboseprint("done")

        return content


    def get_layers_dict(self):
        """
        Return ATT&CK Navigator layers in dict format summarizing detected differences. Returns a dict mapping domain to its layer dict.
        """

        self.verboseprint("generating layers dict... ", end="", flush="true")

        layers = {}
        thedate = datetime.datetime.today().strftime('%B %Y')
        # for each layer file in the domains mapping
        for domain in self.domains:
            # build techniques list
            techniques = []
            used_statuses = set()
            for status in self.data["technique"][domain]:
                if status == "revocations" or status == "deprecations": continue
                for technique in self.data["technique"][domain][status]:
                    for phase in technique['kill_chain_phases']:
                        techniques.append({
                            "techniqueID": technique['external_references'][0]['external_id'],
                            "tactic": phase['phase_name'],
                            "enabled": True,
                            "color": statusToColor[status],
                            "comment": status[:-1] # trim s off end of word
                        })
                        used_statuses.add(status)

            # build legend based off used_statuses
            legendItems = list(map(lambda status: {"color": statusToColor[status], "label": status + ": " + statusDescriptions[status]}, used_statuses))

            # build layer structure
            layer_json = {
                "version": "2.2",
                "name": f"{thedate} {domainToDomainLabel[domain]} Updates",
                "description": f"{domainToDomainLabel[domain]} updates for the {thedate} release of ATT&CK",
                "domain": domainToLayerFileDomain[domain],
                "techniques": techniques,
                "sorting": 0,
                "hideDisabled": False,
                "legendItems": legendItems,
                "showTacticRowBackground": True,
                "tacticRowBackground": "#205b8f",
                "selectTechniquesAcrossTactics": True
            }
            # default to show pre-attack on pre layer
            if domain == "pre-attack": layer_json["filters"] = {
                "stages": ["prepare"]
            }

            layers[domain] = layer_json

        self.verboseprint("done")

        return layers


def markdown_string_to_file(outfile, content):
    """
    Print the string passed in to the indicated output file.
    """

    verboseprint("writing markdown string to file... ", end="", flush="true")

    outfile = open(outfile, "w")
    outfile.write(content)
    outfile.close()

    verboseprint("done")


def layers_dict_to_files(outfiles, layers):
    """
    Print the layers dict passed in to layer files.
    """

    verboseprint("writing layers dict to layer files... ", end="", flush="true")

    # write each layer to separate files
    json.dump(layers['enterprise-attack'], open(outfiles[0], "w"), indent=4)
    json.dump(layers['mobile-attack'], open(outfiles[1], "w"), indent=4)
    json.dump(layers['pre-attack'], open(outfiles[2], "w"), indent=4)

    verboseprint("done")


if __name__ == '__main__':
    old_dir_default = "old"
    date = datetime.datetime.today()
    md_default = os.path.join("output", f"updates-{date.strftime('%B-%Y').lower()}.md")
    layer_defaults = [
        os.path.join("output", f"{date.strftime('%B_%Y')}_Updates_Enterprise.json"),
        os.path.join("output", f"{date.strftime('%B_%Y')}_Updates_Mobile.json"),
        os.path.join("output", f"{date.strftime('%B_%Y')}_Updates_Pre.json")
    ]
    
    parser = argparse.ArgumentParser(
         description="Create -markdown and/or -layers reporting on the changes between two versions of the ATT&CK content. Takes STIX bundles as input. For default operation, put enterprise-attack.json, mobile-attack.json, and pre-attack.json bundles in 'old' and 'new' folders for the script to compare."
    )

    parser.add_argument("-old",
        type=str,
        metavar="OLD_DIR",
        help=f"the directory of the old content. Default is '{old_dir_default}'"
    )

    parser.add_argument("-new",
        type=str,
        metavar="NEW_DIR",
        default="new",
        help="the directory of the new content. Default is '%(default)s'"
    )

    parser.add_argument("-types",
        type=str,
        nargs="+",
        metavar=("OBJ_TYPE", "OBJ_TYPE"),
        choices=[
            "technique", "software", "group", "mitigation"
        ],
        default=[
            "technique", "software", "group", "mitigation"
        ],
        help="which types of objects to report on. Choices (and defaults) are %(choices)s"
    )

    parser.add_argument("-domains",
        type=str,
        nargs="+",
        metavar="DOMAIN",
        choices=[
            "enterprise-attack", "pre-attack", "mobile-attack"
        ],
        default=[
            "enterprise-attack", "pre-attack", "mobile-attack"
        ],
        help="which domains to report on. Choices (and defaults) are %(choices)s"
    )

    parser.add_argument("-markdown",
        type=str,
        nargs="?",
        const=md_default, # default if no value specified
        help="create a markdown file reporting changes. If value is unspecified, defaults to %(const)s"
    )
    
    parser.add_argument("-layers",
        type=str,
        nargs="*",
        # metavar=("ENTERPRISE", "MOBILE", "PRE"),
        help=f'''
             create layer files showing changes in each domain
             expected order of filenames is 'enterprise', 'mobile', 'pre attack'. 
             If values are unspecified, defaults to {", ".join(layer_defaults)}
             '''
    )

    parser.add_argument("-site_prefix",
        type=str,
        default="",
        help="prefix links in markdown output, e.g. [prefix]/techniques/T1484"
    )

    parser.add_argument("-v", "--verbose",
        action="store_true",
        help="print progress bars and status messages"
    )

    parser.add_argument("--minor-changes",
        action="store_true",
        help="show changes to objects which didn't increment the version number"
    )

    parser.add_argument("--use-taxii",
        action="store_true",
        help="Use content from the ATT&CK TAXII server for the -old data"
    )

    parser.add_argument("--show-key",
        action="store_true",
        help="Add a key explaining the change types to the markdown"
    )
    
    args = parser.parse_args()

    if args.use_taxii and args.old is not None:
        parser.error('--use-taxii and -old cannot be used together')
        
    if (not args.markdown and args.layers is None):
        print("Script doesn't output anything unless -markdown and/or -layers are specified. Run 'python3 diff_stix.py -h' for usage instructions")
        exit()

    if args.old is None:
        args.old = old_dir_default

    diffStix = DiffStix(
        domains=args.domains,
        layers=args.layers,
        markdown=args.markdown,
        minor_changes=args.minor_changes,
        new=args.new,
        old=args.old,
        show_key=args.show_key,
        site_prefix=args.site_prefix,
        types=args.types,
        use_taxii=args.use_taxii,
        verbose=args.verbose
    )

    if args.verbose:
        def verboseprint(*args, **kwargs):
                print(*args, **kwargs)
    else:
        verboseprint = lambda *a, **k: None    

    

    if args.markdown:
        md_string = diffStix.get_markdown_string()
        markdown_string_to_file(args.markdown, md_string)

    if args.layers is not None:
        if len(args.layers) is 0:
            # no files specified, e.g. '-layers', use defaults
            diffStix.layers = layer_defaults
            args.layers = layer_defaults
        elif len(args.layers) is 3:
            # files specified, e.g. '-layers file.json file2.json file3.json', use specified
            diffStix.layers = args.layers       # assumes order of files is enterprise, mobile, pre attack (same order as defaults)
        else:
            parser.error('-layers requires exactly three files to be specified or none at all')

        layers_dict = diffStix.get_layers_dict()
        layers_dict_to_files(args.layers, layers_dict)
