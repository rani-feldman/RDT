# Third-party notices

RDT is released under the MIT licence (see LICENSE). The installed RDT and the portable zip also
contain the software below, each under its own licence. The licence texts are in the
`_internal` folder of the installation, next to each package (`*.dist-info/LICENSE*` files).

| Software | Licence | Home |
| --- | --- | --- |
| Python | Python Software Foundation License | https://www.python.org |
| Qt for Python (PySide6, shiboken6) | LGPL-3.0 | https://www.qt.io/qt-for-python |
| Qt 6 | LGPL-3.0 | https://www.qt.io |
| pyqtgraph | MIT | https://www.pyqtgraph.org |
| NumPy (used by pyqtgraph) | BSD-3-Clause | https://numpy.org |
| pyelftools | Public domain (Unlicense) | https://github.com/eliben/pyelftools |
| pySerial | BSD-3-Clause | https://github.com/pyserial/pyserial |
| pyreadline3 | BSD-3-Clause | https://github.com/pyreadline3/pyreadline3 |
| PyInstaller bootloader | GPL-2.0 with a bootloader exception that allows any licence for the app | https://pyinstaller.org |

## Qt and the LGPL

RDT uses Qt and PySide6 under the GNU Lesser General Public License version 3. They are installed
as separate DLL and Python files in the `_internal` folder, not linked into RDT, so you may replace
them with your own build of the same version. The Qt source code is at https://download.qt.io and
the LGPL text at https://www.gnu.org/licenses/lgpl-3.0.html.
