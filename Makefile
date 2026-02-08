# Makefile for LaTeX compilation

# Main document (without .tex extension)
MAIN = main

# LaTeX compiler
LATEX = pdflatex
BIBTEX = bibtex

# All source files
TEXFILES = $(wildcard *.tex)
BIBFILES = $(wildcard *.bib)

.PHONY: all clean distclean

all: $(MAIN).pdf

$(MAIN).pdf: $(MAIN).tex $(TEXFILES) $(BIBFILES)
	$(LATEX) $(MAIN)
	$(BIBTEX) $(MAIN)
	$(LATEX) $(MAIN)
	$(LATEX) $(MAIN)

clean:
	rm -f *.aux *.log *.bbl *.blg *.out *.toc *.lof *.lot *.fls *.fdb_latexmk *.synctex.gz comment.cut

distclean: clean
	rm -f $(MAIN).pdf
