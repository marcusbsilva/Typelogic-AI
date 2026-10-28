"""Assemble a trained 2B checkpoint in a new directory using verified local files."""
import argparse,shutil,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from typelogic.learning import select_checkpoint,checkpoint_preflight
from typelogic.model_profile import prepare_backbone
from tools.install_models import install_trained


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--trained',type=Path,required=True);p.add_argument('--base-dir',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    if a.out.exists():raise ValueError('Use a new output directory')
    select_checkpoint(a.trained);prepare_backbone(a.base_dir)
    a.out.mkdir(parents=True);shutil.copytree(a.base_dir,a.out/'backbone',ignore=shutil.ignore_patterns('.cache'))
    install_trained(a.out,a.trained);checkpoint_preflight(a.out)
    print('Compatible 2B checkpoint assembled. Domain calibration and final testing remain required.')
if __name__=='__main__':main()
