"""Small traced solids with repeated owned instances for real-browser regression."""

from build123d import Box, BuildPart, Compound, Cylinder, Location, Mode

from cadiste.assembly import instance


def block():
    with BuildPart() as part:
        Box(18, 14, 16)
        Cylinder(3, 20, mode=Mode.SUBTRACT)
    return part.part


def peg():
    with BuildPart() as part:
        Cylinder(5, 16)
    return part.part


PARTS = {"Block": block, "Peg": peg}


def build(name="Block"):
    return PARTS[name]()


def assembly():
    return Compound(
        label="Browser fixture",
        children=[
            instance(block(), part="Block", label="block-a", location=Location((-26, 0, 8))),
            instance(block(), part="Block", label="block-b", location=Location((0, 0, 8))),
            instance(peg(), part="Peg", label="peg", location=Location((26, 0, 8))),
        ],
    )
