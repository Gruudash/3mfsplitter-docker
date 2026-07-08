"""Generate a minimal, valid .3mf file for the docker-verify CI smoke test.

A single watertight colored cube -- just enough for /api/split to have a
real file to split, without needing a checked-in binary fixture.
"""

import sys
import zipfile

MODEL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
       xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02">
  <resources>
    <m:colorgroup id="10">
      <m:color color="#ff0000"/>
    </m:colorgroup>
    <object id="1" name="ci_cube" pid="10" p1="0">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0"/><vertex x="5" y="0" z="0"/>
          <vertex x="5" y="5" z="0"/><vertex x="0" y="5" z="0"/>
          <vertex x="0" y="0" z="5"/><vertex x="5" y="0" z="5"/>
          <vertex x="5" y="5" z="5"/><vertex x="0" y="5" z="5"/>
        </vertices>
        <triangles>
          <triangle v1="0" v2="2" v3="1"/><triangle v1="0" v2="3" v3="2"/>
          <triangle v1="4" v2="5" v3="6"/><triangle v1="4" v2="6" v3="7"/>
          <triangle v1="0" v2="1" v3="5"/><triangle v1="0" v2="5" v3="4"/>
          <triangle v1="2" v2="3" v3="6"/><triangle v1="3" v2="7" v3="6"/>
          <triangle v1="0" v2="4" v3="7"/><triangle v1="0" v2="7" v3="3"/>
          <triangle v1="1" v2="2" v3="6"/><triangle v1="1" v2="6" v3="5"/>
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1"/></build>
</model>"""


def main() -> None:
    out_path = sys.argv[1] if len(sys.argv) > 1 else "ci_test.3mf"
    with zipfile.ZipFile(out_path, "w") as zf:
        zf.writestr("3D/3dmodel.model", MODEL_XML)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
