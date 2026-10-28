"""Explicit metadata-only preparation. Model weights must be downloaded manually."""
import argparse,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from typelogic.model_profile import asset_manifest,verify_backbone,asset_path


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--destination',type=Path,required=True);args=parser.parse_args()
    if args.destination.exists():raise ValueError('Use a new destination; no local assets are overwritten')
    from huggingface_hub import hf_hub_download
    manifest=asset_manifest()
    for name in manifest['files']:
        if name.endswith('.safetensors'):continue
        asset_path(args.destination,name)
        hf_hub_download(manifest['repo'],filename=name,revision=manifest['revision'],local_dir=args.destination)
    verify_backbone(args.destination,manifest,metadata_only=True)
    print('Pinned metadata prepared. Download the shard manually, then verify with tools/install_models.py. Startup remains offline.')
if __name__=='__main__':main()
