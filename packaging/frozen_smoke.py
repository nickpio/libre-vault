"""Exercise bundled native libraries, editing, math and a spawned graph worker."""
from io import BytesIO
import multiprocessing
import os
from pathlib import Path
from tempfile import TemporaryDirectory


def main():
    from librevault.app import Gdk, Gio, GLib, Gtk, Window
    from librevault.graph import graph_worker
    from librevault.reading import pixbuf_bytes
    from matplotlib.mathtext import math_to_image
    import cairo
    import gi

    identity = os.environ.get('LIBREVAULT_TEST_CLASS')
    if identity:
        GLib.set_prgname(identity)
        Gdk.set_program_class(identity)
    if not Gtk.init_check()[0]:
        raise RuntimeError('The build smoke test needs a display. Use scripts/check_build.py via check.sh.')
    app = Gtk.Application(application_id='org.librevault.BuildSmoke',
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    with TemporaryDirectory(prefix='librevault-frozen-') as directory:
        base = Path(directory)
        root = base / 'vault'
        root.mkdir()
        (root / 'A.md').write_text('---\naliases: [Alpha]\n---\n# A\n[[B]]\n')
        (root / 'B.md').write_text('# B\n')
        window = Window(app, preferences_path=base / 'settings.json')
        try:
            assert window.vault is None
            window.load_vault(root)
            window.open_note('A.md')
            pane = window.current()
            assert pane.note.path == 'A.md'
            pane.buffer.insert(pane.buffer.get_end_iter(), '\nBuilt from source\n')
            pane.save()
            assert 'Built from source' in (root / 'A.md').read_text()
            assert window.vault.resolve('B.md', 'Alpha') == ['A.md']
            # Exercise the GTK/Cairo foreign-type bridge and bundled image loaders.
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 2, 2)
            Gdk.cairo_set_source_pixbuf(cairo.Context(surface),
                                      Gdk.pixbuf_get_from_surface(surface, 0, 0, 2, 2), 0, 0)
            image = BytesIO()
            math_to_image(r'$\sqrt{x^2 + 1}$', image, format='png')
            assert pixbuf_bytes(image.getvalue()).get_width() > 0
            try:
                gi.require_version('Poppler', '0.18')
                from gi.repository import Poppler
            except (ImportError, ValueError):
                print('Optional PDF preview not bundled.')
            else:
                pdf = base / 'preview.pdf'
                pdf_surface = cairo.PDFSurface(str(pdf), 20, 20)
                cairo.Context(pdf_surface).paint()
                pdf_surface.finish()
                document = Poppler.Document.new_from_file(pdf.as_uri(), None)
                assert document.get_n_pages() == 1
                document.get_page(0).render(cairo.Context(surface))
                print('Bundled Poppler PDF preview passed.')
            context = multiprocessing.get_context('spawn')
            parent, child = context.Pipe()
            process = context.Process(target=graph_worker,
                                      args=(child, str(window.vault.index.path), {}), daemon=True)
            try:
                process.start()
                child.close()
                assert parent.poll(30), 'Frozen graph worker did not respond'
                message = parent.recv()
                assert message[0] == 'graph', message
                assert message[1] == ['A.md', 'B.md'], message
                assert message[2] == [(0, 1)], message
                parent.send('step')
                assert parent.poll(10), 'Frozen graph layout did not respond'
                assert parent.recv()[0] == 'positions'
                parent.send('stop')
                process.join(10)
                assert process.exitcode == 0, process.exitcode
            finally:
                parent.close()
                child.close()
                if process.pid:
                    if process.is_alive():
                        process.terminate()
                    process.join()
                    process.close()
        finally:
            window.destroy()
    print('Frozen build smoke passed: GTK, CSS, edit/save, YAML, links, Cairo, math, images, spawned graph')
    return 0
