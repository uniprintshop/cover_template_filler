"""Additional unseen-layout checks; leaves the original 46-case corpus intact."""
import argparse
from pathlib import Path

from reliability_cases import build_corpus, case, logo, page, text
from thesis_cover_watch import load_config


def specifications():
    partner = page()
    partner["logos"] = [logo("bremen", box=(55, 720, 220, 90)),
                        logo("rwth-aachen", box=(55, 60, 280, 90))]
    partner["text"] += [text("Oberes Logo: Kooperationspartner", y=695),
                        text("Abschlussverleihende Hochschule: unteres Logo", y=175)]
    joint = page()
    joint["logos"] = [logo("rub", box=(55, 720, 220, 90)),
                      logo("rwth-aachen", box=(315, 720, 225, 90))]
    joint["text"].append(text("Jointly awarded by both universities", y=695))
    return [
        case("h01_lower_awarder", "Reverse logo roles and use different institutions.", uid="rwth-aachen", pages=[partner]),
        case("h02_joint_english", "English joint award with different institutions.", uid=None,
             decision="reject", pages=[joint]),
        case("h03_comma_name_birthplace", "Surname-first name plus birthplace in a labelled combined field.",
             uid="bremen", author="Öztürk, Cem", pages=[page("bremen", author="Öztürk, Cem, Essen",
                  author_label="Name, Place of Birth")]),
        case("h04_plain_comma_name", "Surname-first comma without a birthplace label.",
             uid="bremen", author="Öztürk, Cem", author_label="Submitted by:"),
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--output", type=Path, default=Path("reliability_runs/holdouts"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    cfg = load_config(args.config)
    manifest = build_corpus(args.output, Path(cfg["templates_dir"]), cfg["universities"],
                            overwrite=args.overwrite, cases=specifications())
    print(f"Built {len(manifest['cases'])} additional cases in {args.output}")
