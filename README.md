This **XIFS-FITS-VIEWER** tool was born out of necessity.
I actually wanted to have a quick way to view headers in FITS files and to be able to adjust the classic main parameters (e.g. filters). 
Since I mainly work with XIFS files with Pixinsight, I realized that there are no tools at all that can display these images outside of PixInsight.
Additionally I wanted a quick mouseless (keyboard based) way for tmy workflow of walking through the files of my nightly astro sessions. 
So I created a couple of keyboards shortcuts and now mouse-context-menus. There is also a little help window available with some hints.

In the course of the first implementations, a few little things came into play that support my workflow in the initial and pre-selection of captured astrophotographic image files.
support. So in the end it was a bit more than just changing Fitz headers and displaying X IFS files.

The whole code is quick and dirty and does not necessarily correspond to high and development,
but it works quite well so far. If you like, you are welcome to professionalize it.

The **basic way of working** is as follows:

- After starting the viewer, you open the directory in which the images are located.
These are displayed with a slight stretching. The stretching parameters can be adjusted a little. To make things quicker, I have defined some presets that can be called up quickly and easily using the number keys without having to work with the sliders.
-Once you have selected a file on the right with the mouse, you can navigate up and down very easily using the arrow keys. If you want to remove a file but don't want to brutally delete it yet, you can use the backspace key to move it to a directory that I have called Pre Trash. In this way you have a very quick way to perform an image review. If you also perform a backspace within the Pre Trash folder, the file is moved back into the main folder. A real complete deletion does not take place within this tool for security reasons.
-With full format sensors, 120 MB files can quickly accumulate and if you imagine that you have 1020 or 100 of them, loading and displaying these files naturally takes a certain amount of time and makes the process a little slow. That's why I've built in a small catching function, but also a preview mode. So if you press the C key, all the images in the directory are briefly loaded once, which of course takes a little while, during which you can get yourself a coffee. After that you can navigate at high speed using the arrow keys and make your selection.
So far the most important things at this point. 

You can of course open the program using command line parameters. This is probably the quickest way. 
I have also created a monolithic self-executing file for myself with pyinstaller installer. 
This is a little more practical but delays the state of the program considerably. But everyone as they like it.

Greetings
Joerg

Known Issues:
Some xifs files seem to capsule fits headers a bit differently. In such cases a error message will appear and you have to select another by clicking with the mouse.
