This **XIFS-FITS-VIEWER** tool was born out of necessity.
I actually wanted to have a quick way to view headers in FITS files and to be able to adjust the classic main parameters (e.g. filters). 
Since I mainly work with XIFS files with Pixinsight, I realized that there are no tools at all that can display these images outside of PixInsight.
Additionally I wanted a quick mouseless (keyboard based) way for tmy workflow of walking through the files of my nightly astro sessions. 
So I created a couple of keyboards shortcuts and now mouse-context-menus. There is also a little help window available with some hints.

In the course of the first implementations, a few little things came into play that support my workflow in the initial and pre-selection of captured astrophotographic image files.
support. So in the end it was a bit more than just changing Fitz headers and displaying X IFS files.

The whole code is quick and dirty and does not necessarily correspond to high and development,
but it works quite well so far. If you like, you are welcome to professionalize it.

Have a look at the wiki to get further informations.

You might need to install some python packages too, in case you haven't done so.

e.g. Astropy etc.
In that case simply install it by 
pip install astropy
etc.


