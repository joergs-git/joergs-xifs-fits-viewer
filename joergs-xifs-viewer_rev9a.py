import tkinter as tk
from tkinter import filedialog, messagebox, font
import tkinter.font as tkFont
import xml.etree.ElementTree as ET
import numpy as np
from io import BytesIO
from PIL import Image, ImageTk
import zlib
import lz4.block
import os
import glob
import shutil
from collections import OrderedDict
import webbrowser
import threading # Für nebenläufiges Caching

# Für FITS-Unterstützung: installiere via "pip install astropy"
from astropy.io import fits

# Optional: Bitshuffle für Shuffling (pip install bitshuffle)
try:
    import bitshuffle
except ImportError:
    bitshuffle = None

def open_link(url):
    webbrowser.open(url)

def asinh_stretch(img_norm, stretch_factor):
    if stretch_factor <= 0:
        return img_norm
    return np.arcsinh(img_norm * stretch_factor) / np.arcsinh(stretch_factor)

def parse_xisf_header(file_bytes):
    xml_start = file_bytes.find(b"<?xml")
    if xml_start < 0:
        raise ValueError("Kein XML-Header gefunden.")
    xml_end_tag = b"</xisf>"
    xml_end = file_bytes.find(xml_end_tag, xml_start)
    if xml_end < 0:
        raise ValueError("Kein </xisf> Tag gefunden.")
    xml_full = file_bytes[xml_start: xml_end + len(xml_end_tag)]
    root = ET.fromstring(xml_full)
    ns = {"n": "http://www.pixinsight.com/xisf"}
    image_elem = root.find("n:Image", ns)
    if image_elem is None:
        raise ValueError("Kein <Image> Element gefunden.")
    geometry = image_elem.get("geometry")
    width, height, channels = map(int, geometry.split(":"))
    sample_format = image_elem.get("sampleFormat", "UInt16")
    compression = image_elem.get("compression", "none").lower()
    location = image_elem.get("location", "attachment:0:0")
    parts = location.split(":")
    if parts[0] != "attachment":
        raise ValueError("Nur 'attachment'-Location wird unterstützt.")
    data_offset = int(parts[1])
    compressed_size = int(parts[2])
    if sample_format.lower() == "uint16":
        bytes_per_pixel = 2
    else:
        raise ValueError(f"Nur UInt16 wird unterstützt, aber sampleFormat ist {sample_format}")
    if compression != "none":
        try:
            comp_parts = compression.split("+sh:")
            if len(comp_parts) == 2:
                uncompressed_size = int(comp_parts[1].split(":")[0])
                item_size = int(comp_parts[1].split(":")[1])
            else:
                uncompressed_size = width * height * bytes_per_pixel
                item_size = 1
        except Exception:
            uncompressed_size = width * height * bytes_per_pixel
            item_size = 1
    else:
        uncompressed_size = width * height * bytes_per_pixel
        item_size = 1
    return {
        "width": width,
        "height": height,
        "channels": channels,
        "sample_format": sample_format,
        "compression": compression,
        "offset": data_offset,
        "compressed_size": compressed_size,
        "uncompressed_size": uncompressed_size,
        "item_size": item_size,
        "xml_header": xml_full.decode("utf-8", errors="replace")
    }

def unshuffle_uint16(data):
    arr = np.frombuffer(data, dtype=np.uint8)
    n = arr.size // 2
    low = arr[:n].astype(np.uint16)
    high = arr[n:2*n].astype(np.uint16)
    combined = low + (high << 8)
    return combined.tobytes()

class XISFViewer(tk.Tk):
    PREVIEW_MAX_WIDTH = 400 # Maximale Breite der generierten Vorschauen

    def __init__(self):
        super().__init__()
        self.title("XIFS-FITS-Fastselector")
        self.geometry("1200x800")
        self.configure(bg="black")
        
        # Variablen
        self.current_folder = None
        self.last_folder = os.path.join(os.path.expanduser("~"), "Desktop")
        self.active_files = []
        self.pretrash_files = []
        self.current_file = None
        self.current_index = None
        self.original_img_norm = None
        self.xml_header = None
        self.use_aligned = True  
        self.cache = OrderedDict()
        self.last_transform_params = None
        self.cached_transformed = None
        
        self.preview_cache = {}  # Cache für reduzierte Vorschauen
        self.displaying_preview = False # Flag ob gerade eine Vorschau angezeigt wird
        self.preview_generation_in_progress = False # Flag für laufende Preview-Generierung

        # ----- TOP FRAME -----
        top_frame = tk.Frame(self, bg="black")
        top_frame.pack(fill=tk.X, padx=2, pady=2)
        
        title_label1 = tk.Label(top_frame, text="XIFS-FITS-Fastselector by @joergsflow", 
                                fg="red", bg="black", cursor="hand2", font=("Arial", 14, "bold"),
                                activebackground="gray20")
        title_label1.pack(side=tk.LEFT, padx=5)
        title_label1.bind("<Button-1>", lambda e: open_link("https://www.instagram.com/joergsflow/"))
        
        title_label2 = tk.Label(top_frame, text="Astrobin", 
                                fg="red", bg="black", cursor="hand2", font=("Arial", 14, "bold"),
                                activebackground="gray20")
        title_label2.pack(side=tk.LEFT, padx=5)
        title_label2.bind("<Button-1>", lambda e: open_link("https://app.astrobin.com/u/joergsflow#gallery"))
        
        help_btn = tk.Button(top_frame, text="?", fg="red", bg="black", activebackground="gray20",
                             command=self.show_help_popup, font=("Arial", 14, "bold"))
        help_btn.pack(side=tk.RIGHT, padx=5)
        
        edit_fits_btn = tk.Button(top_frame, text="Edit FITS Headers", fg="red", bg="black", activebackground="gray20",
                                  command=self.edit_fits_headers, font=("Arial", 14))
        edit_fits_btn.pack(side=tk.RIGHT, padx=5)
        
        open_folder_btn = tk.Button(top_frame, text="Open Folder", fg="red", bg="black", activebackground="gray20",
                                    command=self.open_folder_dialog, font=("Arial", 14))
        open_folder_btn.pack(side=tk.LEFT, padx=10)
        
        self.status_label = tk.Label(top_frame, text="", fg="yellow", bg="black", font=("Arial", 12))
        self.status_label.pack(side=tk.LEFT, padx=10)

        # Keybindings
        self.bind("0", lambda event: self.apply_preset("0"))
        self.bind("1", lambda event: self.apply_preset("1"))
        self.bind("2", lambda event: self.apply_preset("2"))
        self.bind("3", lambda event: self.apply_preset("3"))
        self.bind("4", lambda event: self.apply_preset("4"))
        
        self.bind("<Up>", self.navigate_up)
        self.bind("<Down>", self.navigate_down)
        self.bind("w", self.navigate_up)
        self.bind("s", self.navigate_down)
        
        self.bind("q", self.skip_up)
        self.bind("a", self.skip_down)

        self.bind("c", self.create_previews_for_current_folder) # Keybind für Preview Caching
        self.bind("C", self.create_previews_for_current_folder) # Auch mit Shift+C

        # ----- MAIN PANEDWINDOW -----
        self.main_paned = tk.PanedWindow(self, orient=tk.HORIZONTAL, bg="black", sashwidth=4)
        self.main_paned.pack(fill=tk.BOTH, expand=True)
        
        self.image_paned = tk.PanedWindow(self.main_paned, orient=tk.VERTICAL, bg="black", sashwidth=4)
        self.main_paned.add(self.image_paned, stretch="always")
        
        self.left_top_frame = tk.Frame(self.image_paned, bg="black")
        self.image_paned.add(self.left_top_frame, stretch="always")
        
        slider_frame1 = tk.Frame(self.left_top_frame, bg="black")
        slider_frame1.pack(fill=tk.X, padx=2, pady=1)
        
        self.stretch_base_slider = tk.Scale(slider_frame1, from_=0, to=100, orient=tk.HORIZONTAL,
                                            label="Stretch Base", resolution=1,
                                            fg="red", bg="black", highlightbackground="black",
                                            troughcolor="gray20", activebackground="gray20",
                                            font=("Arial", 14))
        self.stretch_base_slider.set(10)
        self.stretch_base_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        
        self.multiplier_slider = tk.Scale(slider_frame1, from_=0, to=10000, orient=tk.HORIZONTAL,
                                          label="Multiplier", resolution=1,
                                          fg="red", bg="black", highlightbackground="black",
                                          troughcolor="gray20", activebackground="gray20",
                                          font=("Arial", 14))
        self.multiplier_slider.set(1000)
        self.multiplier_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        
        self.stretch_base_slider.bind("<ButtonRelease-1>", lambda e: self.on_slider_release())
        self.multiplier_slider.bind("<ButtonRelease-1>", lambda e: self.on_slider_release())
        
        slider_frame2 = tk.Frame(self.left_top_frame, bg="black")
        slider_frame2.pack(fill=tk.X, padx=2, pady=1)
        
        self.gamma_slider = tk.Scale(slider_frame2, from_=0.1, to=3.0, resolution=0.1, orient=tk.HORIZONTAL,
                                     label="Gamma", length=200, fg="red", bg="black",
                                     highlightbackground="black", troughcolor="gray20", activebackground="gray20",
                                     font=("Arial", 14))
        self.gamma_slider.set(0.7)
        self.gamma_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        
        self.brightness_slider = tk.Scale(slider_frame2, from_=0.5, to=2.0, resolution=0.1, orient=tk.HORIZONTAL,
                                          label="Brightness", length=200, fg="red", bg="black",
                                          highlightbackground="black", troughcolor="gray20", activebackground="gray20",
                                          font=("Arial", 14))
        self.brightness_slider.set(1.0)
        self.brightness_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        
        self.contrast_slider = tk.Scale(slider_frame2, from_=0.5, to=2.0, resolution=0.1, orient=tk.HORIZONTAL,
                                        label="Contrast", length=200, fg="red", bg="black",
                                        highlightbackground="black", troughcolor="gray20", activebackground="gray20",
                                        font=("Arial", 14))
        self.contrast_slider.set(1.5)
        self.contrast_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        
        self.gamma_slider.bind("<ButtonRelease-1>", lambda e: self.on_slider_release())
        self.brightness_slider.bind("<ButtonRelease-1>", lambda e: self.on_slider_release())
        self.contrast_slider.bind("<ButtonRelease-1>", lambda e: self.on_slider_release())
        
        self.image_label = tk.Label(self.left_top_frame, bg="black", fg="red", font=("Arial", 14))
        self.image_label.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        self.image_label.bind("<Configure>", lambda e: self.update_display_image())
        
        self.left_bottom_frame = tk.Frame(self.image_paned, bg="black")
        self.image_paned.add(self.left_bottom_frame, stretch="never")
        
        header_title = tk.Label(self.left_bottom_frame, text="FITS Header", font=("Arial", 14, "bold"),
                                fg="red", bg="black")
        header_title.pack(anchor=tk.W, padx=2, pady=(2,0))
        
        self.fits_text = tk.Text(self.left_bottom_frame, font=("Arial", 16), height=20, wrap=tk.NONE,
                                 fg="red", bg="black", insertbackground="red",
                                 borderwidth=1, relief="ridge")
        self.fits_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=2, pady=2)
        self.fits_text.tag_configure("white", foreground="white")
        fits_scrollbar = tk.Scrollbar(self.left_bottom_frame, command=self.fits_text.yview, bg="black")
        fits_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.fits_text.config(yscrollcommand=fits_scrollbar.set, state=tk.DISABLED)
        
        self.file_paned = tk.PanedWindow(self.main_paned, orient=tk.VERTICAL, bg="black", sashwidth=4)
        self.main_paned.add(self.file_paned, stretch="always")
        
        active_frame = tk.Frame(self.file_paned, bg="black")
        active_label = tk.Label(active_frame, text="Active Files", font=("Arial", 14, "bold"),
                                fg="red", bg="black")
        active_label.pack(anchor=tk.W, padx=2, pady=(2,0))
        
        self.active_listbox = tk.Listbox(active_frame, font=("Arial", 14), exportselection=False,
                                         selectmode=tk.EXTENDED,
                                         fg="red", bg="black", highlightbackground="black",
                                         selectbackground="gray20", selectforeground="red",
                                         borderwidth=1, relief="ridge", width=75)
        self.active_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        active_scroll = tk.Scrollbar(active_frame, command=self.active_listbox.yview, bg="black")
        active_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.active_listbox.config(yscrollcommand=active_scroll.set)
        self.active_listbox.bind("<<ListboxSelect>>", self.on_active_file_activate)
        self.active_listbox.bind("<Delete>", self.dump_active_file)
        self.active_listbox.bind("<BackSpace>", self.dump_active_file)
        self.active_listbox.bind("<Up>", lambda event: self.navigate_up(event))
        self.active_listbox.bind("<Down>", lambda event: self.navigate_down(event))
        self.active_listbox.bind("w", lambda event: self.navigate_up(event))
        self.active_listbox.bind("s", lambda event: self.navigate_down(event))
        self.active_listbox.bind("q", lambda event: self.skip_up(event))
        self.active_listbox.bind("a", lambda event: self.skip_down(event))
        
        self.file_paned.add(active_frame, stretch="always")
        
        pretrash_frame = tk.Frame(self.file_paned, bg="black")
        pretrash_label = tk.Label(pretrash_frame, text="PRETRASH", font=("Arial", 14, "bold"),
                                  fg="red", bg="black")
        pretrash_label.pack(anchor=tk.W, padx=2, pady=(2,0))
        
        self.pretrash_listbox = tk.Listbox(pretrash_frame, font=("Arial", 14), exportselection=False,
                                           fg="red", bg="black", highlightbackground="black",
                                           selectbackground="gray20", selectforeground="red",
                                           borderwidth=1, relief="ridge", width=75)
        self.pretrash_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        pretrash_scroll = tk.Scrollbar(pretrash_frame, command=self.pretrash_listbox.yview, bg="black")
        pretrash_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.pretrash_listbox.config(yscrollcommand=pretrash_scroll.set)
        self.pretrash_listbox.bind("<<ListboxSelect>>", self.on_pretrash_file_activate)
        self.pretrash_listbox.bind("<Delete>", self.restore_pretrash_file)
        self.pretrash_listbox.bind("<BackSpace>", self.restore_pretrash_file)
        
        self.file_paned.add(pretrash_frame, stretch="always")
        
        self.after(500, self.set_sash_position)

    def on_slider_release(self):
        """Wird aufgerufen, wenn ein Slider losgelassen wird."""
        self.displaying_preview = False # Deaktiviere den Preview-Modus
        self.update_display_image()

    def set_sash_position(self):
        try:
            self.update_idletasks()
            total_width = self.winfo_width()
            if hasattr(self.main_paned, "sashpos"):
                self.main_paned.sashpos(0, total_width // 2)
            total_height_files = self.file_paned.winfo_height()
            if hasattr(self.file_paned, "sashpos"):
                self.file_paned.sashpos(0, int(total_height_files * 0.67))
            total_height_image = self.image_paned.winfo_height()
            if hasattr(self.image_paned, "sashpos"):
                self.image_paned.sashpos(0, int(total_height_image * 0.33))
        except Exception as e:
            print("Sash Exception:", e)

    def open_folder_dialog(self):
        folder = filedialog.askdirectory(initialdir=self.last_folder, title="Select Folder Containing XISF/FITS Files")
        if not folder:
            return
        self.last_folder = folder
        self.current_folder = folder
        
        # Clear existing caches and reset flags
        self.preview_cache.clear()
        self.displaying_preview = False
        self.cache.clear() # Auch den Haupt-LRU-Cache leeren
        self.original_img_norm = None
        self.cached_transformed = None
        self.last_transform_params = None

        files_main = glob.glob(os.path.join(folder, "*.xisf")) + \
                     glob.glob(os.path.join(folder, "*.xifs")) + \
                     glob.glob(os.path.join(folder, "*.fits"))
        files_main = sorted(files_main, key=lambda s: s.lower())
        pretrash_dir = os.path.join(folder, "PRETRASH")
        files_pretrash = []
        if os.path.isdir(pretrash_dir):
            files_pretrash = glob.glob(os.path.join(pretrash_dir, "*.xisf")) + \
                             glob.glob(os.path.join(pretrash_dir, "*.xifs")) + \
                             glob.glob(os.path.join(pretrash_dir, "*.fits"))
            files_pretrash = sorted(files_pretrash, key=lambda s: s.lower())
        self.active_files = files_main
        self.pretrash_files = files_pretrash
        self.update_file_lists()
        self.current_file = None
        self.image_label.configure(image=None)
        self.fits_text.config(state=tk.NORMAL)
        self.fits_text.delete("1.0", tk.END)
        self.fits_text.config(state=tk.DISABLED)
        self.status_label.config(text=f"{len(self.active_files)} files in {os.path.basename(folder)}")

        # Automatically select and load the first file if available
        if self.active_files:
            self.current_index = 0
            self.current_file = self.active_files[0]
            self.active_listbox.selection_clear(0, tk.END)
            self.active_listbox.selection_set(0)
            self.active_listbox.activate(0)
            if self.current_file.lower().endswith(".fits"):
                self.load_fits_file(self.current_file)
            else:
                self.load_xisf_file(self.current_file, use_alignment=True)
        else:
            self.current_index = None


    def update_file_lists(self):
        self.active_listbox.delete(0, tk.END)
        for f in self.active_files:
            try:
                size_bytes = os.path.getsize(f)
                size_mb = round(size_bytes / (1024 * 1024))
            except Exception:
                size_mb = "?"
            display = f"{os.path.basename(f)} ({size_mb} MB)"
            self.active_listbox.insert(tk.END, display)
        self.pretrash_listbox.delete(0, tk.END)
        for f in self.pretrash_files:
            try:
                size_bytes = os.path.getsize(f)
                size_mb = round(size_bytes / (1024 * 1024))
            except Exception:
                size_mb = "?"
            display = f"{os.path.basename(f)} ({size_mb} MB)"
            self.pretrash_listbox.insert(tk.END, display)
        self.active_listbox.config(width=75)
        self.pretrash_listbox.config(width=75)
        # Selection logic moved to open_folder_dialog to ensure consistent state


    def _load_and_select_file(self, file_path, listbox_to_update, index_in_list):
        """Helper to load file and update listbox selection."""
        listbox_to_update.selection_clear(0, tk.END)
        listbox_to_update.selection_set(index_in_list)
        listbox_to_update.activate(index_in_list)
        listbox_to_update.see(index_in_list)

        self.displaying_preview = file_path in self.preview_cache

        if file_path.lower().endswith(".fits"):
            self.load_fits_file(file_path)
        else:
            self.load_xisf_file(file_path, use_alignment=True) # use_alignment ist hier relevant

    def navigate_up(self, event):
        if not self.active_files or self.current_index is None:
            return "break"
        if self.current_index > 0:
            self.current_index -= 1
            self.current_file = self.active_files[self.current_index]
            self._load_and_select_file(self.current_file, self.active_listbox, self.current_index)
        return "break"

    def navigate_down(self, event):
        if not self.active_files or self.current_index is None:
            return "break"
        if self.current_index < len(self.active_files) - 1:
            self.current_index += 1
            self.current_file = self.active_files[self.current_index]
            self._load_and_select_file(self.current_file, self.active_listbox, self.current_index)
        return "break"

    def skip_up(self, event):
        if not self.active_files or self.current_index is None:
            return "break"
        if self.current_index > 0:
            self.current_index -= 1
            self.current_file = self.active_files[self.current_index]
            self.active_listbox.selection_clear(0, tk.END)
            self.active_listbox.selection_set(self.current_index)
            self.active_listbox.activate(self.current_index)
            self.active_listbox.see(self.current_index)
            
            self.displaying_preview = self.current_file in self.preview_cache
            # Nur FITS-Header aktualisieren, Bild aus Cache laden oder Preview anzeigen
            if not self.displaying_preview and self.current_file in self.cache:
                 self.original_img_norm = self.cache[self.current_file]
            elif not self.displaying_preview: # Fallback: wenn nicht im Cache, doch laden
                if self.current_file.lower().endswith(".fits"):
                    self.load_fits_file(self.current_file) # lädt auch original_img_norm
                else:
                    self.load_xisf_file(self.current_file, use_alignment=True)
            
            self.update_fits_header() # Header immer aktualisieren
            self.update_display_image() # Bildanzeige aktualisieren (ggf. mit Preview)
        return "break"

    def skip_down(self, event):
        if not self.active_files or self.current_index is None:
            return "break"
        if self.current_index < len(self.active_files) - 1:
            self.current_index += 1
            self.current_file = self.active_files[self.current_index]
            self.active_listbox.selection_clear(0, tk.END)
            self.active_listbox.selection_set(self.current_index)
            self.active_listbox.activate(self.current_index)
            self.active_listbox.see(self.current_index)

            self.displaying_preview = self.current_file in self.preview_cache
            if not self.displaying_preview and self.current_file in self.cache:
                 self.original_img_norm = self.cache[self.current_file]
            elif not self.displaying_preview:
                if self.current_file.lower().endswith(".fits"):
                    self.load_fits_file(self.current_file)
                else:
                    self.load_xisf_file(self.current_file, use_alignment=True)

            self.update_fits_header()
            self.update_display_image()
        return "break"

    def on_active_file_activate(self, event):
        sel = self.active_listbox.curselection()
        if sel:
            idx = sel[0]
            # Nur laden, wenn sich die Auswahl tatsächlich geändert hat
            if self.current_index != idx or self.current_file != self.active_files[idx]:
                self.current_index = idx
                self.current_file = self.active_files[idx]
                self.displaying_preview = self.current_file in self.preview_cache # Prüfen ob Preview existiert
                if self.current_file.lower().endswith(".fits"):
                    self.load_fits_file(self.current_file)
                else:
                    self.load_xisf_file(self.current_file, use_alignment=True)


    def on_pretrash_file_activate(self, event):
        sel = self.pretrash_listbox.curselection()
        if sel:
            idx = sel[0]
            self.current_index = idx # Hier ist der Index relativ zur pretrash_list
            self.current_file = self.pretrash_files[idx]
            self.displaying_preview = self.current_file in self.preview_cache # Prüfen ob Preview existiert
            if self.current_file.lower().endswith(".fits"):
                self.load_fits_file(self.current_file)
            else:
                self.load_xisf_file(self.current_file, use_alignment=True)

    def _get_raw_image_data(self, file_path):
        """Hilfsfunktion zum Laden der rohen Bilddaten für die Preview-Erstellung."""
        if file_path.lower().endswith((".xisf", ".xifs")):
            with open(file_path, "rb") as f:
                file_bytes = f.read()
            header = parse_xisf_header(file_bytes)
            if header["compression"] != "none":
                comp_data = file_bytes[header["offset"]: header["offset"] + header["compressed_size"]]
                decompressed = lz4.block.decompress(comp_data, uncompressed_size=header["uncompressed_size"])
                if "+sh:" in header["compression"]:
                    decompressed = unshuffle_uint16(decompressed)
                data = decompressed
            else:
                data = file_bytes[header["offset"]: header["offset"] + header["compressed_size"]]
            image_arr = np.frombuffer(data, dtype=np.uint16).reshape((header["height"], header["width"]))
            return image_arr.astype(np.float32) # Konvertiere zu float für Normalisierung
        elif file_path.lower().endswith(".fits"):
            with fits.open(file_path) as hdulist:
                image_data = None
                for hdu in hdulist:
                    if hdu.data is not None:
                        image_data = np.array(hdu.data, dtype=np.float32)
                        break
                if image_data is None:
                    raise ValueError("No image data found in FITS file.")
                return image_data
        else:
            raise ValueError(f"Unsupported file type for preview: {file_path}")


    def _create_single_preview(self, file_path):
        """Erzeugt eine einzelne Vorschau und speichert sie im Cache."""
        try:
            image_arr = self._get_raw_image_data(file_path)
            
            # Normalisieren (0-1)
            img_min = np.nanmin(image_arr) # nanmin für FITS
            img_max = np.nanmax(image_arr) # nanmax für FITS
            if img_max > img_min:
                img_norm_preview = (image_arr - img_min) / (img_max - img_min)
            else:
                img_norm_preview = np.zeros_like(image_arr, dtype=np.float32)

            # Einfacher Stretch für die Vorschau (damit sie nicht zu dunkel ist)
            # Moderater asinh Stretch und Gamma-Anpassung
            stretched_preview = asinh_stretch(img_norm_preview, 50) # Kleinerer Stretch-Faktor für Preview
            stretched_preview = np.power(stretched_preview, 1 / 1.2) # Weniger aggressives Gamma
            
            img_8bit_preview = (np.clip(stretched_preview, 0, 1) * 255).astype(np.uint8)
            pil_preview_full = Image.fromarray(img_8bit_preview, mode='L') # 'L' für Graustufen

            # Verkleinern unter Beibehaltung des Seitenverhältnisses
            original_width, original_height = pil_preview_full.size
            if original_width == 0 or original_height == 0 : return # Ungültige Bildgröße

            aspect_ratio = original_height / original_width
            preview_width = self.PREVIEW_MAX_WIDTH
            preview_height = int(preview_width * aspect_ratio)
            if preview_height == 0 : preview_height = 1 # Mindesthöhe

            pil_preview_resized = pil_preview_full.resize((preview_width, preview_height), Image.Resampling.LANCZOS)
            
            self.preview_cache[file_path] = pil_preview_resized
            # print(f"Cached preview for: {os.path.basename(file_path)}") # Debug
            return True
        except Exception as e:
            print(f"Error creating preview for {os.path.basename(file_path)}: {e}")
            if file_path in self.preview_cache: # Entferne fehlerhafte Einträge
                del self.preview_cache[file_path]
            return False

    def _cache_previews_thread_target(self):
        """Target function for the preview caching thread."""
        self.preview_generation_in_progress = True
        self.status_label.config(text="Caching previews... (0%)")
        self.update_idletasks()

        cached_count = 0
        total_files = len(self.active_files)
        for i, file_path in enumerate(self.active_files):
            if file_path not in self.preview_cache: # Nur cachen, wenn noch nicht vorhanden
                if self._create_single_preview(file_path):
                    cached_count +=1
            else: # Bereits gecached, zähle mit
                cached_count +=1

            progress = int(( (i + 1) / total_files) * 100)
            self.status_label.config(text=f"Caching previews... ({progress}%)")
            self.update_idletasks() # Wichtig, um UI-Updates im Thread zu sehen

        self.status_label.config(text=f"Preview caching complete. {cached_count}/{total_files} previews available.")
        self.preview_generation_in_progress = False
        
        # Wenn das aktuell angezeigte Bild eine Vorschau bekommen hat und noch nicht als Preview angezeigt wird.
        if self.current_file and self.current_file in self.preview_cache and not self.displaying_preview:
            self.displaying_preview = True
            self.update_display_image() # Aktualisiere die Anzeige, um die neue Vorschau zu nutzen

    def create_previews_for_current_folder(self, event=None):
        if not self.current_folder:
            messagebox.showinfo("Info", "Please open a folder first.")
            return
        if self.preview_generation_in_progress:
            messagebox.showinfo("Info", "Preview generation is already in progress.")
            return
        if not self.active_files:
            messagebox.showinfo("Info", "No active files to create previews for.")
            return

        # Start caching in a separate thread to keep UI responsive
        threading.Thread(target=self._cache_previews_thread_target, daemon=True).start()


    def load_xisf_file(self, file_path, use_alignment=False):
        # self.displaying_preview wird bereits in der aufrufenden Funktion gesetzt
        # basierend auf self.preview_cache. Hier nur Logik für volles Laden.
        
        if file_path in self.cache: # Aus Haupt-Cache laden
            self.original_img_norm = self.cache[file_path]
            self.cached_transformed = None
            self.last_transform_params = None
            # XML Header muss trotzdem geladen werden, falls noch nicht geschehen oder anders
            try:
                with open(file_path, "rb") as f_header:
                    file_bytes_header = f_header.read(4096) # Nur Anfang für Header lesen
                header_info = parse_xisf_header(file_bytes_header)
                self.xml_header = header_info["xml_header"]
            except Exception as e:
                self.xml_header = f"Error reading XISF XML header: {e}"
            self.update_display_image()
            return

        try:
            with open(file_path, "rb") as f:
                file_bytes = f.read()
            header = parse_xisf_header(file_bytes)
            self.xml_header = header["xml_header"]
            
            if header["compression"] != "none":
                comp_data = file_bytes[header["offset"]: header["offset"] + header["compressed_size"]]
                decompressed = lz4.block.decompress(comp_data, uncompressed_size=header["uncompressed_size"])
                if "+sh:" in header["compression"]:
                    decompressed = unshuffle_uint16(decompressed)
                data = decompressed
            else:
                data = file_bytes[header["offset"]: header["offset"] + header["compressed_size"]]
            
            image_arr = np.frombuffer(data, dtype=np.uint16).reshape((header["height"], header["width"]))
            
            img_min = image_arr.min()
            img_max = image_arr.max()
            if img_max > img_min:
                img_norm = (image_arr.astype(np.float32) - img_min) / (img_max - img_min)
            else:
                img_norm = np.zeros_like(image_arr, dtype=np.float32)
            
            self.original_img_norm = img_norm
            self.cached_transformed = None
            self.last_transform_params = None
            
            self.cache[file_path] = img_norm
            if len(self.cache) > 5: # LRU Cache-Größe
                self.cache.popitem(last=False)
            
            self.update_display_image()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open XISF file:\n{e}")
            self.original_img_norm = None # Fehlerfall
            self.image_label.configure(image=None) # Bild leeren
            self.image_label.image = None


    def load_fits_file(self, file_path):
        # self.displaying_preview wird bereits in der aufrufenden Funktion gesetzt.
        
        if file_path in self.cache: # Aus Haupt-Cache laden
            self.original_img_norm = self.cache[file_path]
            self.cached_transformed = None
            self.last_transform_params = None
            self.update_display_image()
            return

        try:
            with fits.open(file_path) as hdulist:
                image_data = None
                # header wird in update_fits_header gesetzt
                for hdu in hdulist:
                    if hdu.data is not None:
                        image_data = np.array(hdu.data, dtype=np.float32)
                        break
            if image_data is None:
                messagebox.showerror("Error", "No image data found in this FITS file.")
                return
            
            img_min = np.nanmin(image_data)
            img_max = np.nanmax(image_data)
            if img_max > img_min:
                img_norm = (image_data - img_min) / (img_max - img_min)
            else:
                img_norm = np.zeros_like(image_data, dtype=np.float32)
            
            self.original_img_norm = img_norm
            self.cached_transformed = None
            self.last_transform_params = None
            self.cache[file_path] = img_norm
            if len(self.cache) > 5: # LRU Cache-Größe
                self.cache.popitem(last=False)
            
            self.update_display_image()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open FITS file:\n{e}")
            self.original_img_norm = None
            self.image_label.configure(image=None)
            self.image_label.image = None

    def update_fits_header(self):
        if not self.current_file:
            self.fits_text.config(state=tk.NORMAL)
            self.fits_text.delete("1.0", tk.END)
            self.fits_text.config(state=tk.DISABLED)
            return
        try:
            if self.current_file.lower().endswith(".fits"):
                with fits.open(self.current_file) as hdulist:
                    header = hdulist[0].header # Nur primärer Header
                    white_headers = {"IMAGETYP", "EXPOSURE", "GAIN", "OFFSET", "CAMERAID", "FILTER", "DATE-OBS", "CCD-TEMP", "RA", "DEC", "OBJECT"}
                    self.fits_text.config(state=tk.NORMAL)
                    self.fits_text.delete("1.0", tk.END)
                    for idx, key in enumerate(header):
                        # Versuche Kommentar zu bekommen, falls vorhanden
                        comment = f" / {header.comments[key]}" if header.comments[key] else ""
                        header_str = f"{key} = {header[key]}{comment}\n"
                        self.fits_text.insert(tk.END, header_str)
                        if key in white_headers:
                            start_idx = f"{idx + 1}.0"
                            end_idx = f"{idx + 1}.end"
                            self.fits_text.tag_add("white", start_idx, end_idx)
                    self.fits_text.config(state=tk.DISABLED)
            elif self.current_file.lower().endswith((".xisf", ".xifs")): # XISF
                if self.xml_header: # Verwende den bereits geladenen XML-Header
                    root = ET.fromstring(self.xml_header)
                    # ns = {"nis": "http://www.pixinsight.com/xisf"} # 'nis' ist üblicher, aber Namespace wird direkt verwendet
                    fits_keywords_xpath = ".//{http://www.pixinsight.com/xisf}FITSKeyword"
                    # Suche auch nach Property Elementen als Fallback für einige wichtige Werte
                    property_xpath = ".//{http://www.pixinsight.com/xisf}Property"

                    fits_keywords = root.findall(fits_keywords_xpath)
                    properties = root.findall(property_xpath)

                    white_headers = {"IMAGETYP", "EXPOSURE", "GAIN", "OFFSET", "CAMERAID", "FILTER", "DATE-OBS", "CCD-TEMP", "RA", "DEC", "OBJECT", "Instrument:Camera:Gain", "Instrument:Camera:Offset", "Observation:Time:Start"}
                    
                    self.fits_text.config(state=tk.NORMAL)
                    self.fits_text.delete("1.0", tk.END)
                    
                    line_idx = 0
                    # Zuerst FITSKeywords
                    for kw in fits_keywords:
                        name = kw.get("name", "")
                        value = kw.get("value", "")
                        comment = kw.get("comment", "")
                        line = f"{name} = {value} ({comment})\n"
                        self.fits_text.insert(tk.END, line)
                        if name in white_headers:
                            start_idx = f"{line_idx + 1}.0"
                            end_idx = f"{line_idx + 1}.end"
                            self.fits_text.tag_add("white", start_idx, end_idx)
                        line_idx += 1
                    
                    # Dann Properties (optional, falls nicht schon als FITSKeyword vorhanden)
                    # Hier könnte man eine Logik einbauen, um Duplikate zu vermeiden, aber für die Anzeige ist es oft ok.
                    self.fits_text.insert(tk.END, "\n--- XISF Properties ---\n")
                    line_idx +=2
                    for prop in properties:
                        prop_id = prop.get("id", "")
                        prop_value = prop.get("value", "")
                        prop_type = prop.get("type", "") # Könnte nützlich sein
                        line = f"{prop_id} = {prop_value} [Type: {prop_type}]\n"
                        self.fits_text.insert(tk.END, line)
                        if prop_id in white_headers:
                             start_idx = f"{line_idx + 1}.0"
                             end_idx = f"{line_idx + 1}.end"
                             self.fits_text.tag_add("white", start_idx, end_idx)
                        line_idx += 1

                    self.fits_text.config(state=tk.DISABLED)
                else: # Fallback, falls xml_header nicht gesetzt ist (sollte nicht passieren bei geladenem Bild)
                    self.fits_text.config(state=tk.NORMAL)
                    self.fits_text.delete("1.0", tk.END)
                    self.fits_text.insert(tk.END, "XISF Header information not loaded yet.\n")
                    self.fits_text.config(state=tk.DISABLED)
            else: # Andere Dateitypen
                self.fits_text.config(state=tk.NORMAL)
                self.fits_text.delete("1.0", tk.END)
                self.fits_text.insert(tk.END, "No FITS header information for this file type.\n")
                self.fits_text.config(state=tk.DISABLED)
        except Exception as e:
            self.fits_text.config(state=tk.NORMAL)
            self.fits_text.delete("1.0", tk.END)
            self.fits_text.insert(tk.END, f"Error loading FITS header: {e}\n")
            self.fits_text.config(state=tk.DISABLED)


    def update_display_image(self):
        if self.current_file: # FITS-Header immer aktualisieren, wenn eine Datei ausgewählt ist
            self.update_fits_header()

        pil_image_to_display = None

        if self.displaying_preview and self.current_file and self.current_file in self.preview_cache:
            # Schnelle Vorschau anzeigen
            pil_image_to_display = self.preview_cache[self.current_file]
            # print(f"Displaying PREVIEW for {os.path.basename(self.current_file)}") # Debug
        elif self.original_img_norm is not None:
            # Volles Bild mit Slider-Einstellungen verarbeiten
            try:
                base = float(self.stretch_base_slider.get())
                multiplier = float(self.multiplier_slider.get())
                effective_stretch = base * multiplier
                gamma = float(self.gamma_slider.get())
                brightness = float(self.brightness_slider.get())
                contrast = float(self.contrast_slider.get())
            except ValueError: # Falls Slider-Werte ungültig sind (z.B. bei Initialisierung)
                effective_stretch, gamma, brightness, contrast = 1000, 0.7, 1.0, 1.5 # Default-Werte

            current_params = (effective_stretch, gamma, brightness, contrast)
            
            # Prüfen, ob transformiertes Bild bereits im Cache ist
            if self.last_transform_params is not None and np.allclose(self.last_transform_params, current_params) and self.cached_transformed is not None:
                transformed = self.cached_transformed
            else:
                transformed = asinh_stretch(self.original_img_norm, effective_stretch)
                transformed = np.power(transformed, 1/gamma if gamma != 0 else 1) # Div by zero guard
                transformed = np.clip(transformed * brightness, 0, 1)
                transformed = np.clip((transformed - 0.5) * contrast + 0.5, 0, 1)
                self.cached_transformed = transformed
                self.last_transform_params = current_params
            
            img_scaled = (transformed * 255).clip(0, 255).astype(np.uint8)
            if len(img_scaled.shape) == 2: # Graustufen
                pil_image_to_display = Image.fromarray(img_scaled, mode='L')
            elif len(img_scaled.shape) == 3 and img_scaled.shape[2] == 3: # RGB (falls unterstützt)
                pil_image_to_display = Image.fromarray(img_scaled, mode='RGB')
            else: # Fallback oder unbekanntes Format
                self.image_label.configure(image=None)
                self.image_label.image = None
                # print(f"Cannot create PIL image from shape: {img_scaled.shape}") # Debug
                return
            # print(f"Displaying FULL image for {os.path.basename(self.current_file)}") # Debug
        else:
            # Kein Bild zu laden/anzeigen
            self.image_label.configure(image=None)
            self.image_label.image = None
            return

        if pil_image_to_display:
            try:
                target_width = self.image_label.winfo_width()
                target_height = self.image_label.winfo_height()

                if target_width < 10 or target_height < 10: # Label noch nicht gezeichnet
                    # Fallback auf eine vernünftige Größe, wenn das Label noch nicht initialisiert ist
                    # Dies kann passieren, wenn update_display_image vor dem ersten Configure-Event aufgerufen wird.
                    # Wir nehmen an, das Bild-Label nimmt ca. die Hälfte der Fensterbreite und 1/3 der Höhe ein.
                    parent_width = self.left_top_frame.winfo_width() if self.left_top_frame.winfo_width() > 10 else self.winfo_width() // 2
                    parent_height = self.left_top_frame.winfo_height() if self.left_top_frame.winfo_height() > 10 else self.winfo_height() // 1.5
                    target_width = max(10, parent_width - 10) # Kleiner Puffer
                    target_height = max(10, parent_height - 80) # Puffer für Slider

                orig_width, orig_height = pil_image_to_display.size
                if orig_width == 0 or orig_height == 0: return # Ungültige Bildgröße

                scale = min(target_width / orig_width, target_height / orig_height)
                new_size = (max(1, int(orig_width * scale)), max(1, int(orig_height * scale)))
                
                pil_resized = pil_image_to_display.resize(new_size, Image.Resampling.LANCZOS)
                
                tk_image = ImageTk.PhotoImage(pil_resized)
                self.image_label.configure(image=tk_image)
                self.image_label.image = tk_image # Referenz behalten!
            except Exception as e:
                # print(f"Error resizing or displaying image: {e}") # Debug
                self.image_label.configure(image=None)
                self.image_label.image = None
        else:
            self.image_label.configure(image=None)
            self.image_label.image = None


    def dump_active_file(self, event=None):
        if not self.active_files or self.current_index is None:
            return
        
        selected_indices = self.active_listbox.curselection()
        if not selected_indices: # Falls nichts ausgewählt ist (sollte nicht passieren, wenn current_index gesetzt ist)
            if self.current_index < len(self.active_files): # Versuche mit current_index
                 selected_indices = (self.current_index,)
            else:
                return

        # Dateien von unten nach oben löschen, um Indexprobleme zu vermeiden
        files_to_move_paths = [self.active_files[i] for i in selected_indices]
        
        folder = self.current_folder # Annahme: alle Dateien sind im selben Ordner
        pretrash_dir = os.path.join(folder, "PRETRASH")
        if not os.path.exists(pretrash_dir):
            os.makedirs(pretrash_dir)

        moved_count = 0
        for file_path in sorted(files_to_move_paths, key=lambda p: self.active_files.index(p), reverse=True):
            try:
                # Finde den tatsächlichen Index in der aktuellen Liste (kann sich geändert haben)
                current_idx_of_file = self.active_files.index(file_path)

                shutil.move(file_path, pretrash_dir)
                removed_file_path = self.active_files.pop(current_idx_of_file)
                self.pretrash_files.append(os.path.join(pretrash_dir, os.path.basename(removed_file_path)))
                
                # Aus Caches entfernen
                if removed_file_path in self.cache:
                    del self.cache[removed_file_path]
                if removed_file_path in self.preview_cache:
                    del self.preview_cache[removed_file_path]
                moved_count += 1

            except Exception as e:
                messagebox.showerror("Error", f"{os.path.basename(file_path)} was not moved:\n{e}")
                continue # Mit der nächsten Datei fortfahren

        if moved_count > 0:
            self.active_files.sort(key=lambda s: s.lower()) # Nur nötig wenn nicht alle von pop betroffen sind
            self.pretrash_files.sort(key=lambda s: s.lower())
            self.update_file_lists() # Listboxen aktualisieren

            if self.active_files:
                # Versuche, eine sinnvolle Auswahl zu treffen
                # Wenn der alte current_index noch gültig ist, behalte ihn, sonst nimm den letzten
                if self.current_index >= len(self.active_files):
                    self.current_index = len(self.active_files) - 1
                
                if self.current_index < 0 and self.active_files: # Falls alles davor gelöscht wurde
                    self.current_index = 0

                if self.current_index >= 0 :
                    self.current_file = self.active_files[self.current_index]
                    self.active_listbox.selection_clear(0, tk.END)
                    self.active_listbox.selection_set(self.current_index)
                    self.active_listbox.activate(self.current_index)
                    self.displaying_preview = self.current_file in self.preview_cache
                    if self.current_file.lower().endswith(".fits"):
                        self.load_fits_file(self.current_file)
                    else:
                        self.load_xisf_file(self.current_file, use_alignment=True)
                else: # Keine aktiven Dateien mehr nach dem Löschen
                    self.current_file = None
                    self.original_img_norm = None
                    self.image_label.configure(image=None)
                    self.image_label.image = None
                    self.fits_text.config(state=tk.NORMAL); self.fits_text.delete("1.0", tk.END); self.fits_text.config(state=tk.DISABLED)

            else: # Keine aktiven Dateien mehr
                self.current_file = None
                self.current_index = None
                self.original_img_norm = None
                self.image_label.configure(image=None)
                self.image_label.image = None
                self.fits_text.config(state=tk.NORMAL); self.fits_text.delete("1.0", tk.END); self.fits_text.config(state=tk.DISABLED)
            self.status_label.config(text=f"{len(self.active_files)} files in {os.path.basename(self.current_folder)}")


    def restore_pretrash_file(self, event=None):
        if not self.pretrash_files:
            return
        sel = self.pretrash_listbox.curselection()
        if not sel:
            return
        
        # Von unten nach oben wiederherstellen, um Indexprobleme zu vermeiden
        files_to_restore_paths = [self.pretrash_files[i] for i in sel]
        
        main_folder = self.current_folder # Der Ordner, in den wiederhergestellt wird

        restored_count = 0
        last_restored_file_in_active_list = None

        for file_path in sorted(files_to_restore_paths, key=lambda p: self.pretrash_files.index(p), reverse=True):
            try:
                current_idx_of_file = self.pretrash_files.index(file_path)

                shutil.move(file_path, main_folder)
                restored_file_basename = os.path.basename(file_path)
                self.pretrash_files.pop(current_idx_of_file)
                
                # Füge zur Active-Liste hinzu und sorge dafür, dass sie sortiert bleibt
                # (oder sortiere am Ende einmal)
                newly_restored_path = os.path.join(main_folder, restored_file_basename)
                self.active_files.append(newly_restored_path)
                last_restored_file_in_active_list = newly_restored_path # Merke dir die letzte wiederhergestellte Datei

                # Caches für diese Datei leeren, da sie ggf. neu geladen/gepreviewed werden muss
                if newly_restored_path in self.cache: del self.cache[newly_restored_path]
                if newly_restored_path in self.preview_cache: del self.preview_cache[newly_restored_path]
                
                restored_count += 1
            except Exception as e:
                messagebox.showerror("Error", f"Error while restoring {os.path.basename(file_path)}:\n{e}")
                continue

        if restored_count > 0:
            self.active_files.sort(key=lambda s: s.lower())
            # self.pretrash_files.sort(key=lambda s: s.lower()) # Ist bereits durch pop aktuell
            self.update_file_lists()

            if last_restored_file_in_active_list and last_restored_file_in_active_list in self.active_files:
                self.current_index = self.active_files.index(last_restored_file_in_active_list)
                self.current_file = last_restored_file_in_active_list
                
                self.active_listbox.selection_clear(0, tk.END)
                self.active_listbox.selection_set(self.current_index)
                self.active_listbox.activate(self.current_index)
                self.active_listbox.see(self.current_index) # Scroll to item

                self.displaying_preview = self.current_file in self.preview_cache
                if self.current_file.lower().endswith(".fits"):
                    self.load_fits_file(self.current_file)
                else:
                    self.load_xisf_file(self.current_file, use_alignment=True)
            elif self.active_files: # Falls die letzte wiederhergestellte Datei nicht gefunden wird, nimm die erste
                self.current_index = 0
                self.current_file = self.active_files[0]
                self.active_listbox.selection_clear(0, tk.END)
                self.active_listbox.selection_set(0)
                self.active_listbox.activate(0)
                self.displaying_preview = self.current_file in self.preview_cache
                if self.current_file.lower().endswith(".fits"):
                    self.load_fits_file(self.current_file)
                else:
                    self.load_xisf_file(self.current_file, use_alignment=True)
            self.status_label.config(text=f"{len(self.active_files)} files in {os.path.basename(self.current_folder)}")


    def apply_preset(self, preset):
        if preset == "1": # Default
            self.stretch_base_slider.set(10)
            self.multiplier_slider.set(1000)
            self.gamma_slider.set(0.7)
            self.brightness_slider.set(1.0)
            self.contrast_slider.set(1.5)
        elif preset == "0": # Linear / Minimum
            self.stretch_base_slider.set(0) # Effectively disables asinh_stretch if it checks for > 0
            self.multiplier_slider.set(1)   # Or a very small number if 0 causes issues
            self.gamma_slider.set(1.0)      # Linear gamma
            self.brightness_slider.set(1.0) # Neutral brightness
            self.contrast_slider.set(1.0)   # Neutral contrast
        elif preset == "2": # Medium Stretch
            self.stretch_base_slider.set(20)
            self.multiplier_slider.set(2000)
            self.gamma_slider.set(1.0)
            self.brightness_slider.set(1.0)
            self.contrast_slider.set(1.2)
        elif preset == "3": # High Stretch
            self.stretch_base_slider.set(50)
            self.multiplier_slider.set(5000)
            self.gamma_slider.set(0.5)
            self.brightness_slider.set(1.1)
            self.contrast_slider.set(1.8)
        elif preset == "4": # Max Visual Stretch (Careful, might clip)
            self.stretch_base_slider.set(100)
            self.multiplier_slider.set(10000) # Max
            self.gamma_slider.set(0.4)
            self.brightness_slider.set(1.2)
            self.contrast_slider.set(2.0) # Max
        
        self.displaying_preview = False # Nach Preset-Anwendung immer volles Bild neu berechnen
        self.update_display_image()

    def show_help_popup(self):
        help_win = tk.Toplevel(self)
        help_win.title("Help")
        help_win.configure(bg="black")
        # help_win.geometry("450x400") # Angepasst für neuen Text
        help_text_content = (
            "Usage Instructions:\n\n"
            "1. Open a folder using 'Open Folder'.\n"
            "2. File lists: Active Files and PRETRASH.\n"
            "3. Delete/BackSpace: Move files to/from PRETRASH.\n"
            "   (Select multiple files with Ctrl/Shift+Click before Delete)\n"
            "4. Adjust image parameters with sliders.\n"
            "   Releasing a slider recalculates the full image.\n"
            "5. Navigate (reload image & FITS): Up/Down arrows or W/S.\n"
            "6. Skip FITS headers (no full reload if preview/cache exists):\n"
            "   Q (up) and A (down).\n"
            "7. Press 'C' to generate fast previews for all files in the\n"
            "   current folder. This allows very fast navigation once done.\n"
            "   Previews are shown until a slider is adjusted.\n"
            "8. Edit FITS: Select files, click 'Edit FITS Headers'.\n\n"
            "Preset keys (Number keys 0-4):\n"
            "  1 = Default Stretch\n"
            "  0 = Linear (Minimal Stretch)\n"
            "  2 = Medium Stretch\n"
            "  3 = High Stretch\n"
            "  4 = Max Visual Stretch\n\n"
            "Notes:\n"
            "- Ensure network drives are properly mounted.\n"
            "- For XISF compressed files with shuffling, 'bitshuffle' library\n"
            "  is recommended (pip install bitshuffle).\n"
            "- Preview caching runs in the background. Status is shown.\n"
        )
        label = tk.Label(help_win, text=help_text_content, fg="red", bg="black", font=("Arial", 13), justify="left", anchor="nw") # 13pt, nw anchor
        label.pack(padx=10, pady=10, fill=tk.BOTH, expand=True)
        
        # Dynamische Größenanpassung des Fensters basierend auf dem Text
        help_win.update_idletasks()
        text_width = label.winfo_reqwidth() + 20 # + padding
        text_height = label.winfo_reqheight() + 60 # + padding and button
        help_win.geometry(f"{text_width}x{text_height}")


        close_btn = tk.Button(help_win, text="Close", fg="red", bg="black", activebackground="gray20",
                              command=help_win.destroy, font=("Arial", 14))
        close_btn.pack(pady=5)
        help_win.transient(self) # Macht das Fenster modal relativ zum Hauptfenster
        help_win.grab_set()      # Ergreift den Fokus
        self.wait_window(help_win) # Wartet, bis das Hilfefenster geschlossen wird


    def edit_fits_headers(self):
        sel = self.active_listbox.curselection()
        if not sel:
            messagebox.showerror("Error", "No files selected.")
            return
        selected_files = [self.active_files[idx] for idx in sel]
        
        first_file = selected_files[0]
        filter_value = ""
        imagetyp_value = "LIGHT" # Default
        date_value = "Unknown"
        
        # Versuche, Header der ERSTEN ausgewählten Datei zu lesen
        try:
            if first_file.lower().endswith(".fits"):
                with fits.open(first_file) as hdulist:
                    header = hdulist[0].header
                    filter_value = header.get("FILTER", "")
                    imagetyp_value = header.get("IMAGETYP", "LIGHT")
                    date_value = header.get("DATE-OBS", "Unknown")
            elif first_file.lower().endswith((".xisf", ".xifs")):
                 with open(first_file, "rb") as f:
                    file_bytes = f.read(8192) # Lese nur einen Teil für den Header
                 header_data = parse_xisf_header(file_bytes) # Nutze die existierende Funktion
                 xml_header_str = header_data["xml_header"]
                 root = ET.fromstring(xml_header_str)
                 # Namespaces können variieren, hier ein allgemeiner Ansatz
                 for elem in root.findall(".//*[{http://www.pixinsight.com/xisf}FITSKeyword]"): # Finde FITSKeyword Elemente
                    if elem.get("name") == "FILTER":
                        filter_value = elem.get("value", "").strip("'")
                    if elem.get("name") == "IMAGETYP":
                        imagetyp_value = elem.get("value", "LIGHT").strip("'")
                    if elem.get("name") == "DATE-OBS":
                        date_value = elem.get("value", "Unknown").strip("'")
                 if date_value == "Unknown": # Fallback für XISF-spezifische Zeit
                    creation_time_elem = root.find(".//*[@id='XISF:CreationTime']")
                    if creation_time_elem is not None:
                        date_value = creation_time_elem.get("value", "Unknown")
            else: # Unbekannter Dateityp
                messagebox.showwarning("Warning", f"Cannot read FITS-like headers from {os.path.basename(first_file)} (unsupported type).")
                # Behalte Defaults
        except Exception as e:
            messagebox.showwarning("Warning", f"Could not read FITS headers from {os.path.basename(first_file)}:\n{e}")

        edit_win = tk.Toplevel(self)
        edit_win.title("Edit FITS Headers")
        edit_win.configure(bg="black")
        # edit_win.geometry("250x350") # Etwas breiter

        info_text = f"File: {os.path.basename(first_file)}"
        if len(selected_files) > 1:
            info_text += f"\n(+{len(selected_files)-1} more)"
        info_text += f"\nDate: {date_value}"
        
        info_label = tk.Label(edit_win, text=info_text,
                              fg="red", bg="black", font=("Arial", 12), justify="left")
        info_label.pack(pady=(5,10), padx=10)
        
        filter_label = tk.Label(edit_win, text="FILTER:", fg="red", bg="black", font=("Arial", 12))
        filter_label.pack(pady=(5,0))
        filter_entry = tk.Entry(edit_win, fg="red", bg="black", insertbackground="red", font=("Arial", 12), width=20)
        filter_entry.pack(pady=(0,10))
        filter_entry.insert(0, filter_value)
        
        imagetyp_label = tk.Label(edit_win, text="IMAGETYP:", fg="red", bg="black", font=("Arial", 12))
        imagetyp_label.pack(pady=(5,0))
        imagetyp_options = ["LIGHT", "DARK", "FLAT", "BIAS", "OTHER"] # "OTHER" hinzugefügt
        imagetyp_var = tk.StringVar(value=imagetyp_value if imagetyp_value in imagetyp_options else "LIGHT")
        
        # Style für OptionMenu
        style = {"fg": "red", "bg": "black", "activebackground": "gray20", "font": ("Arial", 12), "highlightthickness":0, "borderwidth":1, "relief":"solid"}
        imagetyp_menu = tk.OptionMenu(edit_win, imagetyp_var, *imagetyp_options)
        imagetyp_menu.config(**style)
        imagetyp_menu["menu"].config(fg="red", bg="black", activebackground="gray20", font=("Arial",11))
        imagetyp_menu.pack(pady=(0,10))
        
        button_frame = tk.Frame(edit_win, bg="black")
        button_frame.pack(pady=10, fill=tk.X, padx=10)

        apply_btn = tk.Button(button_frame, text="Preview & Apply", fg="red", bg="black", activebackground="gray20",
                              command=lambda: self.preview_fits_headers(filter_entry.get(), imagetyp_var.get(), selected_files, edit_win),
                              font=("Arial", 12))
        apply_btn.pack(side=tk.LEFT, expand=True, padx=5)
        
        cancel_btn = tk.Button(button_frame, text="Cancel", fg="red", bg="black", activebackground="gray20",
                               command=edit_win.destroy, font=("Arial", 12))
        cancel_btn.pack(side=tk.RIGHT, expand=True, padx=5)

        edit_win.transient(self)
        edit_win.grab_set()
        edit_win.update_idletasks() # Für korrekte Größenberechnung
        # Zentriere das Popup über dem Hauptfenster
        x = self.winfo_x() + (self.winfo_width() // 2) - (edit_win.winfo_width() // 2)
        y = self.winfo_y() + (self.winfo_height() // 2) - (edit_win.winfo_height() // 2)
        edit_win.geometry(f"+{x}+{y}")
        self.wait_window(edit_win)


    def preview_fits_headers(self, filter_value, imagetyp_value, selected_files, edit_win):
        preview_win = tk.Toplevel(self)
        preview_win.title("Preview FITS Header Changes")
        preview_win.configure(bg="black")
        preview_win.geometry("500x400") # Breiter für bessere Übersicht
        
        preview_text_frame = tk.Frame(preview_win, bg="black")
        preview_text_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        preview_text = tk.Text(preview_text_frame, font=("Monaco", 11), wrap=tk.NONE, fg="red", bg="black", # Monospaced Font
                               insertbackground="red", borderwidth=1, relief="solid") # Solider Rand
        preview_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        preview_scroll_y = tk.Scrollbar(preview_text_frame, command=preview_text.yview, bg="black", troughcolor="gray20")
        preview_scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        preview_text.config(yscrollcommand=preview_scroll_y.set)

        preview_scroll_x = tk.Scrollbar(preview_win, command=preview_text.xview, orient=tk.HORIZONTAL, bg="black", troughcolor="gray20")
        preview_scroll_x.pack(side=tk.BOTTOM, fill=tk.X, padx=5)
        preview_text.config(xscrollcommand=preview_scroll_x.set)

        white_headers = {"IMAGETYP", "FILTER"} # Nur die geänderten hervorheben
        preview_text.tag_configure("white", foreground="white", font=("Monaco", 11, "bold"))
        preview_text.tag_configure("filename", foreground="yellow", font=("Monaco", 11, "bold"))
        preview_text.tag_configure("arrow", foreground="cyan")
        preview_text.tag_configure("nochange", foreground="gray60")


        changes_to_apply = [] # (file_path, 'FILTER'/'IMAGETYP', old_val, new_val)

        for file_path in selected_files:
            preview_text.insert(tk.END, f"File: {os.path.basename(file_path)}\n", "filename")
            
            old_filter_val, old_imagetyp_val = "Not set", "Not set"
            is_fits = file_path.lower().endswith(".fits")
            is_xisf = file_path.lower().endswith((".xisf", ".xifs"))

            try:
                if is_fits:
                    with fits.open(file_path) as hdul:
                        header = hdul[0].header
                        old_filter_val = header.get("FILTER", "Not set")
                        old_imagetyp_val = header.get("IMAGETYP", "Not set")
                elif is_xisf:
                    with open(file_path, "rb") as f_xisf:
                        xml_header_str = parse_xisf_header(f_xisf.read(8192))["xml_header"]
                    root = ET.fromstring(xml_header_str)
                    for elem in root.findall(".//*[{http://www.pixinsight.com/xisf}FITSKeyword]"):
                        if elem.get("name") == "FILTER": old_filter_val = elem.get("value", "Not set").strip("'")
                        if elem.get("name") == "IMAGETYP": old_imagetyp_val = elem.get("value", "Not set").strip("'")
                else: # Sollte nicht passieren, da wir nur .fits/.xisf bearbeiten
                    preview_text.insert(tk.END, "  Unsupported file type for header editing.\n")
                    preview_text.insert(tk.END, "-" * 60 + "\n")
                    continue

                # FILTER
                final_filter_val = filter_value.strip() # Neuer Wert aus Eingabefeld
                if final_filter_val == "" and old_filter_val != "Not set" : # Wenn Feld leer, behalte alten Wert, außer alter Wert war "Not set"
                    final_filter_val = old_filter_val
                
                if old_filter_val != final_filter_val:
                    preview_text.insert(tk.END, "  FILTER:   '", "white")
                    preview_text.insert(tk.END, f"{old_filter_val}", "nochange" if old_filter_val=="Not set" else "")
                    preview_text.insert(tk.END, "'", "white")
                    preview_text.insert(tk.END, "  ->  '", "arrow")
                    preview_text.insert(tk.END, f"{final_filter_val}", "white")
                    preview_text.insert(tk.END, "'\n", "white")
                    changes_to_apply.append({'file': file_path, 'keyword': 'FILTER', 'new_value': final_filter_val, 'is_fits': is_fits, 'is_xisf': is_xisf})
                else:
                    preview_text.insert(tk.END, f"  FILTER:   '{old_filter_val}' (no change)\n", "nochange")

                # IMAGETYP
                final_imagetyp_val = imagetyp_value # Neuer Wert aus Dropdown
                if old_imagetyp_val != final_imagetyp_val:
                    preview_text.insert(tk.END, "  IMAGETYP: '", "white")
                    preview_text.insert(tk.END, f"{old_imagetyp_val}", "nochange" if old_imagetyp_val=="Not set" else "")
                    preview_text.insert(tk.END, "'", "white")
                    preview_text.insert(tk.END, "  ->  '", "arrow")
                    preview_text.insert(tk.END, f"{final_imagetyp_val}", "white")
                    preview_text.insert(tk.END, "'\n", "white")
                    changes_to_apply.append({'file': file_path, 'keyword': 'IMAGETYP', 'new_value': final_imagetyp_val, 'is_fits': is_fits, 'is_xisf': is_xisf})
                else:
                    preview_text.insert(tk.END, f"  IMAGETYP: '{old_imagetyp_val}' (no change)\n", "nochange")

            except Exception as e:
                preview_text.insert(tk.END, f"  Error reading/parsing {os.path.basename(file_path)}: {e}\n")
            
            preview_text.insert(tk.END, "-" * 60 + "\n")
        
        preview_text.config(state=tk.DISABLED)
        
        button_frame_preview = tk.Frame(preview_win, bg="black")
        button_frame_preview.pack(pady=5, fill=tk.X, padx=10)

        if not changes_to_apply:
             no_changes_label = tk.Label(button_frame_preview, text="No changes to apply.", fg="yellow", bg="black", font=("Arial", 12))
             no_changes_label.pack(side=tk.LEFT, padx=5)
        else:
            confirm_btn = tk.Button(button_frame_preview, text=f"Confirm {len(changes_to_apply)} Change(s)", fg="red", bg="black", activebackground="gray20",
                                    command=lambda: self.apply_fits_headers_confirmed(changes_to_apply, edit_win, preview_win, selected_files),
                                    font=("Arial", 12))
            confirm_btn.pack(side=tk.LEFT, expand=True, padx=5)
        
        cancel_btn = tk.Button(button_frame_preview, text="Cancel", fg="red", bg="black", activebackground="gray20",
                               command=preview_win.destroy, font=("Arial", 12))
        cancel_btn.pack(side=tk.RIGHT, expand=True, padx=5)

        preview_win.transient(edit_win) # Modal zum Edit-Fenster
        preview_win.grab_set()
        preview_win.update_idletasks()
        x = edit_win.winfo_x() + (edit_win.winfo_width() // 2) - (preview_win.winfo_width() // 2)
        y = edit_win.winfo_y() + (edit_win.winfo_height() // 2) - (preview_win.winfo_height() // 2)
        preview_win.geometry(f"+{x}+{y}")
        self.wait_window(preview_win)


    def apply_fits_headers_confirmed(self, changes, edit_win, preview_win, selected_files_paths_for_reload):
        success_count = 0
        fail_count = 0
        
        for change_item in changes:
            file_path = change_item['file']
            keyword = change_item['keyword']
            new_value = change_item['new_value']
            is_fits_file = change_item['is_fits']
            is_xisf_file = change_item['is_xisf']

            try:
                if is_fits_file:
                    with fits.open(file_path, mode='update') as hdulist:
                        hdulist[0].header[keyword] = new_value
                        hdulist.flush() # Speichert Änderungen in die Datei
                    success_count +=1
                elif is_xisf_file:
                    # XISF: XML parsen, ändern, neu schreiben (komplexer)
                    # Dies erfordert das Lesen der gesamten Datei, Ändern des XML-Teils und Zurückschreiben.
                    # Vorsicht: Dies ist eine heikle Operation. Backup empfohlen.
                    with open(file_path, "rb") as f:
                        content_bytes = f.read()
                    
                    # Finde XML Header Grenzen
                    xml_start_token = b"<?xml"
                    xml_end_token = b"</xisf>"
                    xml_start_offset = content_bytes.find(xml_start_token)
                    xml_end_offset = content_bytes.find(xml_end_token)

                    if xml_start_offset == -1 or xml_end_offset == -1:
                        raise ValueError("Could not find full XML header in XISF.")
                    
                    xml_end_offset += len(xml_end_token) # Inklusive End-Tag

                    xml_string_original = content_bytes[xml_start_offset:xml_end_offset].decode('utf-8')
                    
                    # Parse XML
                    root = ET.fromstring(xml_string_original)
                    ns_xisf = "http://www.pixinsight.com/xisf" # Namespace explizit
                    
                    keyword_found_and_updated = False
                    # Versuche, existierendes Keyword zu aktualisieren
                    for kw_elem in root.findall(f".//{{{ns_xisf}}}FITSKeyword[@name='{keyword}']"):
                        kw_elem.set("value", str(new_value)) # Sicherstellen, dass Wert ein String ist
                        keyword_found_and_updated = True
                        break # Annahme: Nur ein Keyword mit diesem Namen ist relevant oder das erste gefundene
                    
                    if not keyword_found_and_updated:
                        # Keyword nicht gefunden, füge es hinzu
                        # Finde das <Image> oder ein anderes passendes Elternelement, um Keywords anzuhängen
                        image_element = root.find(f".//{{{ns_xisf}}}Image")
                        if image_element is None: # Fallback, falls <Image> nicht direkt unter root ist
                            image_element = root 
                        
                        new_kw_elem = ET.Element(f"{{{ns_xisf}}}FITSKeyword")
                        new_kw_elem.set("name", keyword)
                        new_kw_elem.set("value", str(new_value))
                        new_kw_elem.set("comment", f"Set by XISFViewer") # Optionaler Kommentar
                        image_element.append(new_kw_elem) # Hänge es an <Image> oder root an

                    # XML zurück in String umwandeln
                    # ET.register_namespace('', ns_xisf) # Für sauberes XML ohne 'ns0:' Präfixe, falls nötig
                    modified_xml_string = ET.tostring(root, encoding='utf-8', method='xml').decode('utf-8')
                    
                    # Stelle sicher, dass der XML-Header mit <?xml version="1.0" encoding="UTF-8"?> beginnt
                    if not modified_xml_string.startswith("<?xml"):
                        modified_xml_string = '<?xml version="1.0" encoding="UTF-8"?>\n' + modified_xml_string

                    modified_xml_bytes = modified_xml_string.encode('utf-8')

                    # Kombiniere den neuen Header mit dem Rest der Datei
                    new_content_bytes = content_bytes[:xml_start_offset] + modified_xml_bytes + content_bytes[xml_end_offset:]

                    with open(file_path, "wb") as f_write:
                        f_write.write(new_content_bytes)
                    success_count +=1
                else:
                    fail_count += 1 # Sollte nicht passieren
                    continue

                # Header-Cache für die bearbeitete Datei invalidieren, damit er neu gelesen wird
                if file_path == self.current_file:
                    self.xml_header = None # Für XISF
                # Auch den normalen Bild-Cache invalidieren, falls das Laden des Headers dort Infos setzt
                if file_path in self.cache:
                    del self.cache[file_path]
                # Preview-Cache nicht unbedingt nötig, da Previews keine Header-Infos zeigen
                
            except Exception as e:
                fail_count += 1
                messagebox.showerror("Error Applying Change", f"Failed to update '{keyword}' for {os.path.basename(file_path)}:\n{e}")
        
        edit_win.destroy()
        preview_win.destroy()
        
        summary_message = f"Header changes applied.\nSuccessful: {success_count}\nFailed: {fail_count}"
        if fail_count > 0:
            messagebox.showwarning("FITS Header Update Summary", summary_message)
        else:
            messagebox.showinfo("FITS Header Update Summary", summary_message)

        # Lade die letzte ausgewählte Datei neu, um die Änderungen im UI zu sehen
        if selected_files_paths_for_reload:
            last_file_processed = selected_files_paths_for_reload[-1] # Die letzte Datei aus der ursprünglichen Auswahl
            if last_file_processed in self.active_files: # Sicherstellen, dass sie noch in der aktiven Liste ist
                self.current_index = self.active_files.index(last_file_processed)
                self.current_file = last_file_processed
                
                self.active_listbox.selection_clear(0, tk.END)
                self.active_listbox.selection_set(self.current_index)
                self.active_listbox.activate(self.current_index)
                
                self.displaying_preview = self.current_file in self.preview_cache # Check für Preview
                if self.current_file.lower().endswith(".fits"):
                    self.load_fits_file(self.current_file)
                elif self.current_file.lower().endswith((".xisf", ".xifs")):
                     self.load_xisf_file(self.current_file, use_alignment=True) # use_alignment kann hier wichtig sein
                # update_fits_header und update_display_image werden von den load_xxx_file Methoden aufgerufen.
            elif self.active_files: # Fallback: Lade die erste Datei, wenn die letzte nicht mehr da ist
                self.current_index = 0
                self.current_file = self.active_files[0]
                # ... (restliche Ladelogik wie oben)


if __name__ == "__main__":
    app = XISFViewer()
    app.mainloop()
