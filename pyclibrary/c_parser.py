# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# Copyright 2015 by PyCLibrary Authors, see AUTHORS for more details.
#
# Distributed under the terms of the MIT/X11 license.
#
# The full license is in the file LICENCE, distributed with this software.
# -----------------------------------------------------------------------------
"""
Used for extracting data such as macro definitions, variables, typedefs, and
function signatures from C header files.

"""
from __future__ import (division, unicode_literals, print_function,
                        absolute_import)

import sys
import re
import os
import logging
from inspect import cleandoc
from future.utils import istext
from ast import literal_eval
from traceback import format_exc

logger = logging.getLogger(__name__)


__all__ = ['win_defs', 'CParser']


def win_defs(verbose=False):
    """Loads selection of windows headers included with PyCLibrary.

    These definitions can either be accessed directly or included before
    parsing another file like this:
        windefs = CParser.winDefs()
        p = CParser.CParser("headerFile.h", copyFrom=windefs)

    Definitions are pulled from a selection of header files included in Visual
    Studio (possibly not legal to distribute? Who knows.), some of which have
    been abridged because they take so long to parse.

    """
    headerFiles = ['WinNt.h', 'WinDef.h', 'WinBase.h', 'BaseTsd.h', 'WTypes.h',
                   'WinUser.h']
    d = os.path.dirname(__file__)
    p = CParser(
        [os.path.join(d, 'headers', h) for h in headerFiles],
        types={'__int64': ('long long')},
        macros={'_WIN32': '', '_MSC_VER': '800', 'CONST': 'const',
                'NO_STRICT': None},
        processAll=False
        )

    p.processAll(cache=os.path.join(d, 'headers', 'WinDefs.cache'),
                 noCacheWarning=True, verbose=verbose)

    return p


class CParser(object):
    """Class for parsing C code to extract variable, struct, enum, and function
    declarations as well as preprocessor macros.

    This is not a complete C parser; instead, it is meant to simplify the
    process of extracting definitions from header files in the absence of a
    complete build system. Many files will require some amount of manual
    intervention to parse properly (see 'replace' and extra arguments)

    Parameters
    ----------
    files : str or iterable, optional
        File or files which should be parsed.

    replace : dict, optional
        Specify som string replacements to perform before parsing. Format is
        {'searchStr': 'replaceStr', ...}

    copy_from : CParser or iterable of CParser, optional
        CParser whose definitions should be included.

    process_all : bool, optional
        Flag indicating whether files should be parsed immediatly. True by
        default.

    cache :

    verbose :

    *args :
        Extra parameters may be used to specify the starting state of the
        parser. For example, one could provide a set of missing type
        declarations by types={'UINT': ('unsigned int'), 'STRING': ('char', 1)}
        Similarly, preprocessor macros can be specified: macros={'WINAPI': ''}

    Example
    -------
    Create parser object, load two files

    >>> p = CParser(['header1.h', 'header2.h'])

    Remove comments, preprocess, and search for declarations

    >>> p.process_ all()

    Just to see what was successfully parsed from the files

    >>> p.print_all()

    Access parsed declarations

    >>> all_values = p.defs['values']
    >>> functionSignatures = p.defs['functions']

    To see what was not successfully parsed

    >>> unp = p.process_all(return_unparsed=True)
    >>> for s in unp:
            print s

    """
    #: Increment every time cache structure or parsing changes to invalidate
    #: old cache files.
    cacheVersion = 22

    def __init__(self, files=None, replace=None, copy_from=None,
                 process_all=True, cache=None, **args):

        # Holds all definitions
        self.defs = {}
        # Holds definitions grouped by the file they came from
        self.file_defs = {}

        self.init_opts = args.copy()
        self.init_opts['files'] = []
        self.init_opts['replace'] = {}

        self.data_list = ['types', 'variables', 'fnmacros', 'macros',
                          'structs', 'unions', 'enums', 'functions', 'values']

        self.file_order = []
        self.files = {}

        # Description of the struct packing rules as defined by #pragma pack
        self.pack_list = {}
        if files is not None:
            if type(files) is str:
                files = [files]
            for f in files:
                self.load_file(f, replace)

        # Initialize empty definition lists
        for k in self.data_list:
            self.defs[k] = {}

        # Holds translations from typedefs/structs/unions to fundamental types
        self.compiled_types = {}

        self.current_file = None

        # Import extra arguments if specified
        for t in args:
            for k in args[t].keys():
                self.add_def(t, k, args[t][k])

        # Import from other CParsers if specified
        if copy_from is not None:
            if not isinstance(copy_from, (list, tuple)):
                copy_from = [copy_from]
            for p in copy_from:
                self.import_dict(p.file_defs)

        if process_all:
            self.process_all(cache=cache)

    def process_all(self, cache=None, return_unparsed=False,
                    print_after_preprocess=False):
        """ Remove comments, preprocess, and parse declarations from all files.

        This operates in memory, and thus does not alter the original files.

        Parameters
        ----------
        cache : unicode, optional
            File path where cached results are be stored or retrieved. The
            cache is automatically invalidated if any of the arguments to
            __init__ are changed, or if the C files are newer than the cache.
        return_unparsed : bool, optional
           Passed directly to parse_defs.

        print_after_preprocess : bool, optional
            If true prints the result of preprocessing each file.

        Returns
        -------
        results : list
            List of the results from parse_defs.

        """
        if cache is not None and self.load_cache(cache, check_validity=True):
            logger.debug("Loaded cached definitions; will skip parsing.")
            # Cached values loaded successfully, nothing left to do here
            return

        results = []
        logger.debug(cleandoc('''Parsing C header files (no valid cache found).
                              This could take several minutes...'''))
        for f in self.file_order:

            if self.files[f] is None:
                # This means the file could not be loaded and there was no
                # cache.
                mess = 'Could not find header file "{}" or a cache file.'
                raise Exception(mess.format(f))

            logger.debug("Removing comments from file '{}'...".format(f))
            self.remove_comments(f)

            logger.debug("Preprocessing file '{}'...".format(f))
            self.preprocess(f)

            if print_after_preprocess:
                print("===== PREPROCSSED {} =======".format(f))
                print(self.files[f])

            logger.debug("Parsing definitions in file '{}'...".format(f))

            results.append(self.parse_defs(f, return_unparsed))

        if cache is not None:
            logger.debug("Writing cache file '{}'".format(cache))
            self.write_cache(cache)

        return results

    def load_cache(self, cache_file, check_validity=False):
        """Load a cache file.

        Used internally if cache is specified in process_all().

        Parameters
        ----------
        cache_file : unicode
            Path of the file from which the cache should be loaded.

        check_validity : bool, optional
         If True, then run several checks before loading the cache:
           - cache file must not be older than any source files
           - cache file must not be older than this library file
           - options recorded in cache must match options used to initialize
             CParser

        Returns
        -------
        result : bool
            Did the loading succeeded.

        """

        # Make sure cache file exists
        if not istext(cache_file):
            raise Exception("Cache file option must be a string.")
        if not os.path.isfile(cache_file):
            # If file doesn't exist, search for it in this module's path
            d = os.path.dirname(__file__)
            cache_file = os.path.join(d, "headers", cache_file)
            if not os.path.isfile(cache_file):
                logger.debug("Can't find requested cache file.")
                return False

        # Make sure cache is newer than all input files
        if check_validity:
            mtime = os.stat(cache_file).st_mtime
            for f in self.file_osrder:
                # If file does not exist, then it does not count against the
                # validity of the cache.
                if os.path.isfile(f) and os.stat(f).st_mtime > mtime:
                    logger.debug("Cache file is out of date.")
                    return False

        try:
            # Read cache file
            import pickle
            cache = pickle.load(open(cache_file, 'rb'))

            # Make sure __init__ options match
            if check_validity:
                if cache['opts'] != self.init_opts:
                    db = logger.debug
                    db("Cache file is not valid")
                    db("It was created using different initialization options")
                    db('{}'.format(cache['opts']))
                    db('{}'.format(self.init_opts))
                    return False

                else:
                    logger.debug("Cache init opts are OK:")
                    logger.debug('{}'.format(cache['opts']))

                if cache['version'] < self.cache_version:
                    mess = "Cache file is not valid--cache format has changed."
                    logger.debug(mess)
                    return False

            # Import all parse results
            self.import_dict(cache['file_defs'])
            return True

        except Exception:
            logger.exception("Warning--cache read failed:")
            return False

    def import_dict(self, data):
        """Import definitions from a dictionary.

        The dict format should be the same as CParser.file_defs.
        Used internally; does not need to be called manually.

        """
        for f in data.keys():
            self.current_file = f
            for k in self.data_list:
                for n in data[f][k]:
                    self.add_def(k, n, data[f][k][n])

    def write_cache(self, cache_file):
        """Store all parsed declarations to cache. Used internally.

        """
        cache = {}
        cache['opts'] = self.init_opts
        cache['file_defs'] = self.file_defs
        cache['version'] = self.cache_version
        import pickle
        pickle.dump(cache, open(cache_file, 'wb'))

    def load_file(self, path, replace=None):
        """Read a file, make replacements if requested.

        Called by __init__, should not be called manually.

        Parameters
        ----------
        path : unicode
            Path of the file to load.

        replace : dict, optional
            Dictionary containing strings to replace by the associated value
            when loading the file.

        """
        if not os.path.isfile(path):
            # Not a fatal error since we might be able to function properly if
            # there is a cache file.
            mess = "Warning: C header '{}' is missing, this may cause trouble."
            logger.warning(mess.format(path))
            self.files[path] = None
            return False

        # U causes all newline types to be converted to \n
        with open(path, 'rU') as fd:
            self.files[path] = fd.read()

        if replace is not None:
            for s in replace:
                self.files[path] = re.sub(s, replace[s], self.files[path])

        self.file_order.append(path)
        bn = os.path.basename(path)
        self.init_opts['replace'][bn] = replace
        # Only interested in the file names, the directory may change between
        # systems.
        self.init_opts['files'].append(bn)
        return True

    # =========================================================================
    # --- Processing functions
    # =========================================================================

    def assert_pyparsing(self):
        """Make sure pyparsing module is available."""
        global HAS_PYPARSING
        if not HAS_PYPARSING:
            mess = cleandoc('''CParser class requires 'pyparsing' library for
                            actual parsing work. Without this library, CParser
                            can only be used with previously cached parse
                            results.''')
            raise Exception(mess)

    def remove_comments(self, path):
        """Remove all comments from file.

        Operates in memory, does not alter the original files.

        """
        self.assert_pyparsing()
        text = self.files[path]
        cplusplus_line_comment = Literal("//") + restOfLine
        # match quoted strings first to prevent matching comments inside quotes
        comment_remover = (quotedString | cStyleComment.suppress() |
                           cplusplus_line_comment.suppress())
        self.files[path] = comment_remover.transformString(text)

    def preprocess(self, path):
        """Scan named file for preprocessor directives, removing them while
        expanding macros.

        Operates in memory, does not alter the original files.

        Currently support :
        - conditionals : ifdef, ifndef, if, elif, else (defined can be used
                         in a if statement).
        - definition : define, undef
        - pragmas : pragma

        """
        self.assert_pyparsing()
        # We need this so that eval_expr works properly
        self.build_parser()
        self.current_file = path

        # Stack for #pragma pack push/pop
        pack_stack = [(None, None)]
        self.pack_list[path] = [(0, None)]
        packing = None  # Current packing value

        text = self.files[path]

        # First join together lines split by \\n
        text = Literal('\\\n').suppress().transformString(text)

        # Define the structure of a macro definition
        name = Word(alphas+'_', alphanums+'_')('name')
        deli_list = Optional(lparen + delimitedList(name) + rparen)
        self.pp_define = (name.setWhitespaceChars(' \t')("macro") +
                          deli_list.setWhitespaceChars(' \t')('args') +
                          SkipTo(LineEnd())('value'))
        self.pp_define.setParseAction(self.process_macro_defn)

        # Comb through lines, process all directives
        lines = text.split('\n')

        result = []

        directive = re.compile(r'\s*#([a-zA-Z]+)(.*)$')
        if_true = [True]
        if_hit = []
        for i, line in enumerate(lines):
            new_line = ''
            m = directive.match(line)

            # Regular code line
            if m is None:
                # Only include if we are inside the correct section of an IF
                # block
                if if_true[-1]:
                    new_line = self.expand_macros(line)

            # Macro line
            else:
                d = m.groups()[0]
                rest = m.groups()[1]

                if d == 'ifdef':
                    d = 'if'
                    rest = 'defined ' + rest
                elif d == 'ifndef':
                    d = 'if'
                    rest = '!defined ' + rest

                # Evaluate 'defined' operator before expanding macros
                if d in ['if', 'elif']:
                    def pa(t):
                        is_macro = t['name'] in self.defs['macros']
                        is_macro_func = t['name'] in self.defs['fnmacros']
                        return ['0', '1'][is_macro or is_macro_func]

                    rest = (Keyword('defined') +
                            (name | lparen + name + rparen)
                            ).setParseAction(pa).transformString(rest)

                elif d in ['define', 'undef']:
                    match = re.match(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)(.*)$', rest)
                    macroName, rest = match.groups()

                # Expand macros if needed
                if rest is not None and (all(if_true) or d in ['if', 'elif']):
                    rest = self.expand_macros(rest)

                if d == 'elif':
                    if if_hit[-1] or not all(if_true[:-1]):
                        ev = False
                    else:
                        ev = self.eval_preprocessor_expr(rest)

                    logger.debug("  "*(len(if_true)-2) + line +
                                 '{}, {}'.format(rest, ev))

                    if_true[-1] = ev
                    if_hit[-1] = if_hit[-1] or ev

                elif d == 'else':
                    logger.debug("  "*(len(if_true)-2) + line +
                                 '{}'.format(not if_hit[-1]))
                    if_true[-1] = (not if_hit[-1]) and all(if_true[:-1])
                    if_hit[-1] = True

                elif d == 'endif':
                    if_true.pop()
                    if_hit.pop()
                    logger.debug("  "*(len(if_true)-1) + line)

                elif d == 'if':
                    if all(if_true):
                        ev = self.eval_preprocessor_expr(rest)
                    else:
                        ev = False
                    logger.debug("  "*(len(if_true)-1) + line +
                                 '{}, {}'.format(rest, ev))
                    if_true.append(ev)
                    if_hit.append(ev)

                elif d == 'define':
                    if not if_true[-1]:
                        continue
                    logger.debug("  "*(len(if_true)-1) + "define: " +
                                 '{}, {}'.format(macroName, rest))
                    try:
                        # Macro is registered here
                        self.pp_define.parseString(macroName + ' ' + rest)
                    except Exception:
                        logger.exception("Error processing macro definition:" +
                                         '{}, {}'.format(macroName, rest))

                elif d == 'undef':
                    if not if_true[-1]:
                        continue
                    try:
                        self.rem_def('macros', macroName.strip())
                    except Exception:
                        if sys.exc_info()[0] is not KeyError:
                            mess = "Error removing macro definition '{}'"
                            logger.exception(mess.format(macroName.strip()))

                # Check for changes in structure packing
                elif d == 'pragma':
                    if not if_true[-1]:
                        continue
                    m = re.match(r'\s+pack\s*\(([^\)]+)\)', rest)
                    if m is None:
                        continue
                    opts = [s.strip() for s in m.groups()[0].split(',')]

                    pushpop = id = val = None
                    for o in opts:
                        if o in ['push', 'pop']:
                            pushpop = o
                        elif o.isdigit():
                            val = int(o)
                        else:
                            id = o

                    if val is not None:
                        packing = val

                    if pushpop == 'push':
                        pack_stack.append((packing, id))
                    elif opts[0] == 'pop':
                        if id is None:
                            pack_stack.pop()
                        else:
                            ind = None
                            for i, s in enumerate(pack_stack):
                                if s[1] == id:
                                    ind = i
                                    break
                            if ind is not None:
                                pack_stack = pack_stack[:ind]
                        if val is None:
                            packing = pack_stack[-1][0]
                    else:
                        packing = int(opts[0])

                    mess = ">> Packing changed to {} at line {}"
                    logger.debug(mess.format(str(packing), i))
                    self.pack_list[path].append((i, packing))
                else:
                    # Ignore any other directives
                    mess = 'Ignored directive {} at line {}'
                    logger.debug(mess.format(d, i))

            result.append(new_line)
        self.files[path] = '\n'.join(result)

    def eval_preprocessor_expr(self, expr):
        # Make a few alterations so the expression can be eval'd
        macro_diffs = (
            Literal('!').setParseAction(lambda: ' not ') |
            Literal('&&').setParseAction(lambda: ' and ') |
            Literal('||').setParseAction(lambda: ' or ') |
            Word(alphas + '_', alphanums + '_').setParseAction(lambda: '0'))
        expr2 = macro_diffs.transformString(expr).strip()

        try:
            ev = bool(eval(expr2))
        except Exception:
            mess = "Error evaluating preprocessor expression: {} [{}]\n{}"
            logger.debug(mess.format(expr, repr(expr2), format_exc()))
            ev = False
        return ev

    def process_macro_defn(self, t):
        """Parse a #define macro and register the definition.

        """
        logger.debug("Processing MACRO: {}".format(t))
        macro_val = t.value.strip()
        if macro_val in self.defs['fnmacros']:
            self.add_def('fnmacros', t.macro, self.defs['fnmacros'][macro_val])
            logger.debug("  Copy fn macro {} => {}".format(macro_val, t.macro))

        else:
            if t.args == '':
                val = self.eval_expr(macro_val)
                self.add_def('macros', t.macro, macro_val)
                self.add_def('values', t.macro, val)
                mess = "  Add macro: {} ({}); {}"
                logger.debug(mess.format(t.macro, val,
                                         self.defs['macros'][t.macro]))

            else:
                self.add_def('fnmacros', t.macro,
                             self.compile_fn_macro(macro_val,
                                                   [x for x in t.args]))
                mess = "  Add fn macro: {} ({}); {}"
                logger.debug(mess.format(t.macro, t.args,
                                         self.defs['fnmacros'][t.macro]))

        return "#define " + t.macro + " " + macro_val

    def compile_fn_macro(self, text, args):
        """Turn a function macro spec into a compiled description.

        """
        # Find all instances of each arg in text.
        args_str = '|'.join(args)
        arg_regex = re.compile(r'("(\\"|[^"])*")|(\b({})\b)'.format(args_str))
        start = 0
        parts = []
        arg_order = []
        # The group number to check for macro names
        N = 3
        for m in arg_regex.finditer(text):
            arg = m.groups()[N]
            if arg is not None:
                parts.append(text[start:m.start(N)] + '{}')
                start = m.end(N)
                arg_order.append(args.index(arg))
        parts.append(text[start:])
        return (''.join(parts), arg_order)

    def expand_macros(self, line):
        """
        """
        reg = re.compile(r'("(\\"|[^"])*")|(\b(\w+)\b)')
        parts = []
        start = 0
        # The group number to check for macro names
        N = 3
        macros = self.defs['macros']
        fnmacros = self.defs['fnmacros']
        for m in reg.finditer(line):
            name = m.groups()[N]
            if name in macros:
                parts.append(line[start:m.start(N)])
                start = m.end(N)
                parts.append(macros[name])

        parts.append(line[start:])
        line = ''.join(parts)
        parts = []
        start = 0
        for m in reg.finditer(line):
            name = m.groups()[N]
            if name in fnmacros:
                # If function macro expansion fails, just ignore it.
                try:
                    exp, end = self.expand_fn_macro(name, line[m.end(N):])
                    parts.append(line[start:m.start(N)])
                    start = end + m.end(N)
                    parts.append(exp)
                except:
                    if sys.exc_info()[1][0] != 0:
                        mess = "Function macro expansion failed: {}, {}"
                        logger.error(mess.format(name, line[m.end(N):]))
                        raise

        parts.append(line[start:])
        return ''.join(parts)

    def expand_fn_macro(self, name, text):
        """
        """
        # defn looks like ('%s + %s / %s', (0, 0, 1))
        defn = self.defs['fnmacros'][name]

        arg_list = (stringStart + lparen +
                    Group(delimitedList(expression))('args') + rparen)
        res = [x for x in arg_list.scanString(text, 1)]
        if len(res) == 0:
            mess = "Function macro '{}' not followed by (...)"
            raise Exception(0,  mess.format(name))

        args, start, end = res[0]
        new_str = defn[0].format(*[args[0][i] for i in defn[1]])

        return (new_str, end)

    def parse_defs(self, path, return_unparsed=False):
        """Scan through the named file for variable, struct, enum, and function
        declarations.

        Parameters
        ----------
        path : unicode
            Path of the file to parse for definitions.

        return_unparsed : bool, optional
            If true, return a string of all lines that failed to match (for
            debugging purposes).

        Returns
        -------
        tokens : list
            Entire tree of successfully parsed tokens.

        """
        self.assert_pyparsing()
        self.current_file = path

        parser = self.build_parser()
        if return_unparsed:
            text = parser.suppress().transformString(self.files[path])
            return re.sub(r'\n\s*\n', '\n', text)
        else:
            return [x[0] for x in parser.scanString(self.files[path])]

    def build_parser(self):
        """Builds the entire tree of parser elements for the C language (the
        bits we support, anyway).

        """
        if hasattr(self, 'parser'):
            return self.parser

        self.assert_pyparsing()

        self.struct_type = Forward()
        self.enum_type = Forward()
        desc = (fund_type |
                Optional(kwl(size_modifiers+sign_modifiers)) + ident |
                self.struct_type |
                self.enum_type
                )
        self.type_spec = (type_qualifier + desc + type_qualifier + ms_modifier
                          ).setParseAction(recombine)

        # --- Abstract declarators for use in function pointer arguments
        #   Thus begins the extremely hairy business of parsing C declarators.
        #   Whomever decided this was a reasonable syntax should probably never
        #   breed.
        #   The following parsers combined with the processDeclarator function
        #   allow us to turn a nest of type modifiers into a correctly
        #   ordered list of modifiers.

        self.declarator = Forward()
        self.abstract_declarator = Forward()

        #  Abstract declarators look like:
        #     <empty string>
        #     *
        #     **[num]
        #     (*)(int, int)
        #     *( )(int, int)[10]
        #     ...etc...
        self.abstract_declarator << Group(
            type_qualifier + Group(ZeroOrMore('*'))('ptrs') + type_qualifier +
            ((Optional('&')('ref')) |
             (lparen + self.abstract_declarator + rparen)('center')) +
            Optional(lparen +
                     Optional(delimitedList(Group(
                              self.type_spec('type') +
                              self.abstract_declarator('decl') +
                              Optional(Literal('=').suppress() + expression,
                                       default=None)('val')
                              )), default=None) +
                     rparen)('args') +
            Group(ZeroOrMore(lbrack + Optional(expression, default='-1') +
                  rbrack))('arrays')
        )

        # Declarators look like:
        #     varName
        #     *varName
        #     **varName[num]
        #     (*fnName)(int, int)
        #     * fnName(int arg1=0)[10]
        #     ...etc...
        self.declarator << Group(
            type_qualifier + call_conv + Group(ZeroOrMore('*'))('ptrs') +
            type_qualifier +
            ((Optional('&')('ref') + ident('name')) |
             (lparen + self.declarator + rparen)('center')) +
            Optional(lparen +
                     Optional(delimitedList(
                         Group(self.type_spec('type') +
                               (self.declarator |
                                self.abstract_declarator)('decl') +
                               Optional(Literal('=').suppress() +
                               expression, default=None)('val')
                               )),
                              default=None) +
                     rparen)('args') +
            Group(ZeroOrMore(lbrack + Optional(expression, default='-1') +
                  rbrack))('arrays')
        )
        self.declarator_list = Group(delimitedList(self.declarator))

        # Typedef
        self.type_decl = (Keyword('typedef') + self.type_spec('type') +
                          self.declarator_list('decl_list') + semi)
        self.type_decl.setParseAction(self.process_typedef)

        # Variable declaration
        self.variable_decl = (
            Group(self.type_spec('type') +
                  Optional(self.declarator_list('decl_list')) +
                  Optional(Literal('=').suppress() +
                           (expression('value') |
                            (lbrace +
                             Group(delimitedList(expression))('arrayValues') +
                             rbrace
                             )
                            )
                           )
                  ) +
            semi)
        self.variable_decl.setParseAction(self.process_variable)

        # Function definition
        self.typeless_function_decl = (self.declarator('decl') +
                                       nestedExpr('{', '}').suppress())
        self.function_decl = (self.type_spec('type') +
                              self.declarator('decl') +
                              nestedExpr('{', '}').suppress())
        self.function_decl.setParseAction(self.process_function)

        # Struct definition
        self.struct_decl = Forward()
        struct_kw = (Keyword('struct') | Keyword('union'))
        self.struct_member = (
            Group(self.variable_decl.copy().setParseAction(lambda: None)) |
            (self.type_spec + self.declarator +
             nestedExpr('{', '}')).suppress() |
            (self.declarator + nestedExpr('{', '}')).suppress()
            )
        self.decl_list = (lbrace +
                          Group(OneOrMore(self.struct_member))('members') +
                          rbrace)
        self.struct_type << (struct_kw('struct_type') +
                             ((Optional(ident)('name') +
                               self.decl_list) | ident('name'))
                             )
        self.struct_type.setParseAction(self.process_struct)

        self.struct_decl = self.struct_type + semi

        # Enum definition
        enum_var_decl = Group(ident('name') +
                              Optional(Literal('=').suppress() +
                              (integer('value') | ident('valueName'))))

        self.enum_type << (Keyword('enum') +
                           (Optional(ident)('name') +
                            lbrace +
                            Group(delimitedList(enum_var_decl))('members') +
                            rbrace | ident('name'))
                           )
        self.enum_type.setParseAction(self.process_enum)
        self.enum_decl = self.enum_type + semi

        self.parser = (self.type_decl | self.variable_decl |
                       self.function_decl)
        return self.parser

    def process_declarator(self, decl):
        """Process a declarator (without base type) and return a tuple
        (name, [modifiers])

        See process_type(...) for more information.

        """
        toks = []
        name = None
        logger.debug("DECL: {}".format(decl))
        if 'call_conv' in decl and len(decl['call_conv']) > 0:
            toks.append(decl['call_conv'])

        if 'ptrs' in decl and len(decl['ptrs']) > 0:
            toks.append('*' * len(decl['ptrs']))

        if 'arrays' in decl and len(decl['arrays']) > 0:
            toks.append([self.eval_expr(x) for x in decl['arrays']])

        if 'args' in decl and len(decl['args']) > 0:
            if decl['args'][0] is None:
                toks.append(())
            else:
                toks.append(tuple([self.process_type(a['type'], a['decl']) +
                                   (a['val'][0],) for a in decl['args']]
                                  )
                            )
        if 'ref' in decl:
            toks.append('&')

        if 'center' in decl:
            (n, t) = self.processDeclarator(decl['center'][0])
            if n is not None:
                name = n
            toks.extend(t)

        if 'name' in decl:
            name = decl['name']

        return (name, toks)

    def process_type(self, typ, decl):
        """Take a declarator + base type and return a serialized name/type
        description.

        The description will be a list of elements (name, [basetype, modifier,
        modifier, ...])
          - name is the string name of the declarator or None for an abstract
            declarator
          - basetype is the string representing the base type
          - modifiers can be:
             '*'    - pointer (multiple pointers "***" allowed)
             '&'    - reference
             '__X'  - calling convention (windows only). X can be 'cdecl' or
                      'stdcall'
             list   - array. Value(s) indicate the length of each array, -1 for
                      incomplete type.
             tuple  - function, items are the output of processType for each
                      function argument.

        Examples:
            int *x[10]            =>  ('x', ['int', [10], '*'])
            char fn(int x)         =>  ('fn', ['char', [('x', ['int'])]])
            struct s (*)(int, int*)   =>  (None, ["struct s", ((None, ['int']),
                                           (None, ['int', '*'])), '*'])
        """
        logger.debug("PROCESS TYPE/DECL: {}/{}".format(typ, decl))
        (name, decl) = self.process_declarator(decl)
        return (name, [typ] + decl)

    def process_enum(self, s, l, t):
        """
        """
        try:
            logger.debug("ENUM: {}".format(t))
            if t.name == '':
                n = 0
                while True:
                    name = 'anonEnum{}'.format(n)
                    if name not in self.defs['enums']:
                        break
                    n += 1
            else:
                name = t.name[0]

            logger.debug("  name: {}".format(name))

            if name not in self.defs['enums']:
                i = 0
                enum = {}
                for v in t.members:
                    if v.value != '':
                        # XXXX test with literal_eval
                        i = literal_eval(v.value)
                    if v.valueName != '':
                        i = enum[v.valueName]
                    enum[v.name] = i
                    self.add_def('values', v.name, i)
                    i += 1
                logger.debug("  members: {}".format(enum))
                self.add_def('enums', name, enum)
                self.add_def('types', 'enum '+name, ('enum', name))
            return ('enum ' + name)
        except:
            logger.exception("Error processing enum: {}".format(t))

    def process_function(self, s, l, t):
        logger.debug("FUNCTION {} : {}".format(t, t.keys()))

        try:
            (name, decl) = self.process_type(t.type, t.decl[0])
            if len(decl) == 0 or type(decl[-1]) != tuple:
                logger.error('{}'.format(t))
                mess = "Incorrect declarator type for function definition."
                raise Exception(mess)
            logger.debug("  name: {}".format(name))
            logger.debug("  sig: {}".format(decl))
            self.add_def('functions', name, (decl[:-1], decl[-1]))

        except Exception:
            logger.exception("Error processing function: {}".format(t))

    def packing_at(self, line):
        """Return the structure packing value at the given line number.

        """
        packing = None
        for p in self.pack_list[self.current_file]:
            if p[0] <= line:
                packing = p[1]
            else:
                break
        return packing

    def process_struct(self, s, l, t):
        """
        """
        try:
            str_typ = t.struct_type  # struct or union

            # Check for extra packing rules
            packing = self.packing_at(lineno(l, s))

            logger.debug('{} {} {}'.format(str_typ.upper(), t.name, t))
            if t.name == '':
                n = 0
                while True:
                    sname = 'anon_{}{}'.format(str_typ, n)
                    if sname not in self.defs[str_typ+'s']:
                        break
                    n += 1
            else:
                if istext(t.name):
                    sname = t.name
                else:
                    sname = t.name[0]
            logger.debug("  NAME: {}".format(sname))
            if (len(t.members) > 0 or sname not in self.defs[str_typ+'s'] or
                    self.defs[str_typ+'s'][sname] == {}):
                logger.debug("  NEW " + str_typ.upper())
                struct = []
                for m in t.members:
                    typ = m[0].type
                    val = self.eval_expr(m)
                    logger.debug("    member: {}, {}, {}".format(
                                 m, m[0].keys(), m[0].decl_list))
                    if len(m[0].decl_list) == 0:  # anonymous member
                        struct.append((None, [typ], None))
                    for d in m[0].decl_list:
                        (name, decl) = self.process_type(typ, d)
                        struct.append((name, decl, val))
                        logger.debug("      {} {} {}".format(name, decl, val))
                self.add_def(str_typ+'s', sname,
                             {'pack': packing, 'members': struct})
                self.add_def('types', str_typ+' '+sname, (str_typ, sname))
            return str_typ + ' ' + sname

        except Exception:
            logger.exception('Error processing struct: {}'.format(t))

    def process_variable(self, s, l, t):
        logger.debug("VARIABLE: {}".format(t))
        try:
            val = self.eval_expr(t[0])
            for d in t[0].decl_list:
                (name, typ) = self.process_type(t[0].type, d)
                # This is a function prototype
                if type(typ[-1]) is tuple:
                    logger.debug("  Add function prototype: {} {} {}".format(
                                 name, typ, val))
                    self.add_def('functions', name, (typ[:-1], typ[-1]))
                # This is a variable
                else:
                    logger.debug("  Add variable: {} {} {}".format(name,
                                 typ, val))
                    self.add_def('variables', name, (val, typ))
                    self.add_def('values', name, val)

        except Exception:
            logger.exception('Error processing variable: {}'.format(t))

    def process_typedef(self, s, l, t):
        """
        """
        logger.debug("TYPE: {}".format(t))
        typ = t.type
        for d in t.decl_list:
            (name, decl) = self.process_type(typ, d)
            logger.debug("  {} {}".format(name, decl))
            self.add_def('types', name, decl)

    def eval_expr(self, toks):
        """Evaluates expressions.

        Currently only works for expressions that also happen to be valid
        python expressions. This function does not currently include previous
        variable declarations, but that should not be too difficult to
        implement...

        """
        logger.debug("Eval: {}".format(toks))
        try:
            if istext(toks):
                val = self.eval(toks, None, self.defs['values'])
            elif toks.arrayValues != '':
                val = [self.eval(x, None, self.defs['values'])
                       for x in toks.arrayValues]
            elif toks.value != '':
                val = self.eval(toks.value, None, self.defs['values'])
            else:
                val = None
            return val

        except Exception:
            logger.exception("    failed eval: {}".format(toks))
            return None

    def eval(self, expr, *args):
        """Just eval with a little extra robustness."""
        expr = expr.strip()
        cast = (lparen + self.type_spec + self.abstract_declarator +
                rparen).suppress()
        expr = (quotedString | number | cast).transformString(expr)
        if expr == '':
            return None
        return eval(expr, *args)

    def print_all(self, filename=None):
        """Print everything parsed from files. Useful for debugging.

        Parameters
        ----------
        filename : unicode, optional
            Name of the file whose definition should be printed.

        """
        from pprint import pprint
        for k in self.data_list:
            print("============== {} ==================".format(k))
            if filename is None:
                pprint(self.defs[k])
            else:
                pprint(self.file_defs[filename][k])

    def add_def(self, typ, name, val):
        """Add a definition of a specific type to both the definition set for
        the current file and the global definition set.

        """
        self.defs[typ][name] = val
        if self.current_file is None:
            base_name = None
        else:
            base_name = os.path.basename(self.current_file)
        if base_name not in self.file_defs:
            self.file_defs[base_name] = {}
            for k in self.data_list:
                self.file_defs[base_name][k] = {}
        self.file_defs[base_name][typ][name] = val

    def rem_def(self, typ, name):
        """Remove a definition of a specific type to both the definition set for
        the current file and the global definition set.

        """
        if self.current_file is None:
            base_name = None
        else:
            base_name = os.path.basename(self.current_file)
        del self.defs[typ][name]
        del self.file_defs[base_name][typ][name]

    def is_fund_type(self, typ):
        """Return True if this type is a fundamental C type, struct, or
        union.

        """
        if (typ[0][:7] == 'struct ' or typ[0][:6] == 'union ' or
                typ[0][:5] == 'enum '):
            return True

        names = base_types + size_modifiers + sign_modifiers
        for w in typ[0].split():
            if w not in names:
                return False
        return True

    def eval_type(self, typ):
        """Evaluate a named type into its fundamental type.

        """
        used = []
        while True:
            if self.is_fund_type(typ):
                # Remove 'signed' before returning evaluated type
                typ[0] = re.sub(r'\bsigned\b', '', typ[0]).strip()
                return typ

            parent = typ[0]
            if parent in used:
                m = 'Recursive loop while evaluating types. (typedefs are {})'
                raise Exception(m.format(' -> '.join(used+[parent])))

            used.append(parent)
            if parent not in self.defs['types']:
                m = 'Unknown type "{}" (typedefs are {})'
                raise Exception(m.format(parent, ' -> '.join(used)))
            pt = self.defs['types'][parent]
            typ = pt + typ[1:]

    def find(self, name):
        """Search all definitions for the given name.

        """
        res = []
        for f in self.file_defs:
            fd = self.file_defs[f]
            for t in fd:
                typ = fd[t]
                for k in typ:
                    if istext(name):
                        if k == name:
                            res.append((f, t))
                    else:
                        if re.match(name, k):
                            res.append((f, t, k))
        return res

    def find_text(self, text):
        """Search all file strings for text, return matching lines.

        """
        res = []
        for f in self.files:
            l = self.files[f].split('\n')
            for i in range(len(l)):
                if text in l[i]:
                    res.append((f, i, l[i]))
        return res


HAS_PYPARSING = False
try:
    from .thirdparty.pyparsing import \
        (ParserElement, ParseResults, Forward, Optional, Word, WordStart,
         WordEnd, Keyword, Regex, Literal, SkipTo, ZeroOrMore, OneOrMore,
         Group, LineEnd, stringStart, quotedString, oneOf, nestedExpr,
         delimitedList, restOfLine, cStyleComment, alphas, alphanums, hexnums,
         lineno)
    ParserElement.enablePackrat()
    HAS_PYPARSING = True
except:
    # No need to do anything yet as we might not be using any parsing
    # functions.
    pass


# Define some common language elements if pyparsing is available.
if HAS_PYPARSING:
    # Some basic definitions
    expression = Forward()
    pexpr = '(' + expression + ')'
    num_types = ['int', 'float', 'double', '__int64']
    base_types = ['char', 'bool', 'void'] + num_types
    size_modifiers = ['short', 'long']
    sign_modifiers = ['signed', 'unsigned']
    qualifiers = ['const', 'static', 'volatile', 'inline', 'restrict', 'near',
                  'far']
    ms_modifiers = ['__based', '__declspec', '__fastcall', '__restrict',
                    '__sptr', '__uptr', '__w64', '__unaligned',
                    '__nullterminated']
    keywords = (['struct', 'enum', 'union', '__stdcall', '__cdecl'] +
                qualifiers + base_types + size_modifiers + sign_modifiers)

    def kwl(strs):
        """Generate a match-first list of keywords given a list of strings."""
        return Regex(r'\b(%s)\b' % '|'.join(strs))

    keyword = kwl(keywords)
    wordchars = alphanums+'_$'
    ident = (WordStart(wordchars) + ~keyword +
             Word(alphas + "_", alphanums + "_$") +
             WordEnd(wordchars)).setParseAction(lambda t: t[0])

    semi   = Literal(";").ignore(quotedString).suppress()
    lbrace = Literal("{").ignore(quotedString).suppress()
    rbrace = Literal("}").ignore(quotedString).suppress()
    lbrack = Literal("[").ignore(quotedString).suppress()
    rbrack = Literal("]").ignore(quotedString).suppress()
    lparen = Literal("(").ignore(quotedString).suppress()
    rparen = Literal(")").ignore(quotedString).suppress()
    hexint = Regex('-?0[xX][{}]+[UL]*'.format(hexnums)).setParseAction(lambda t: t[0].rstrip('UL'))
    decint = Regex('-?[0-9]+[UL]*').setParseAction(lambda t: t[0].rstrip('UL'))
    integer = (hexint | decint)
    floating = Regex(r'-?((\d+(\.\d*)?)|(\.\d+))([eE]-?\d+)?')
    number = (integer | floating)
    bitfieldspec = ":" + integer
    bi_operator = oneOf("+ - / * | & || && ! ~ ^ % == != > < >= <= -> . :: << >> = ? :")
    uni_right_operator = oneOf("++ --")
    uni_left_operator = oneOf("++ -- - + * sizeof new")
    name = (WordStart(wordchars) + Word(alphas+"_", alphanums+"_$") +
            WordEnd(wordchars))

    call_conv = Optional(Keyword('__cdecl') | Keyword('__stdcall'))('call_conv')

    # Removes '__name' from all type specs. may cause trouble.
    underscore_2_ident = (WordStart(wordchars) + ~keyword + '__' +
                          Word(alphanums, alphanums+"_$") +
                          WordEnd(wordchars)).setParseAction(lambda t: t[0])
    type_qualifier = ZeroOrMore((underscore_2_ident + Optional(nestedExpr())) |
                                kwl(qualifiers)).suppress()

    ms_modifier = ZeroOrMore(kwl(ms_modifiers) +
                             Optional(nestedExpr())).suppress()
    pointer_operator = ('*' + type_qualifier |
                        '&' + type_qualifier |
                        '::' + ident + type_qualifier
                        )

    # Language elements
    fund_type = OneOrMore(kwl(sign_modifiers + size_modifiers +
                          base_types)).setParseAction(lambda t: ' '.join(t))

    # Is there a better way to process expressions with cast operators??
    cast_atom = (
        ZeroOrMore(uni_left_operator) + Optional('('+ident+')').suppress() +
        ((ident + '(' + Optional(delimitedList(expression)) + ')' |
          ident + OneOrMore('[' + expression + ']') |
          ident | number | quotedString
          ) |
         ('(' + expression + ')')) +
        ZeroOrMore(uni_right_operator)
        )

    uncast_atom = (
        ZeroOrMore(uni_left_operator) +
        ((ident + '(' + Optional(delimitedList(expression)) + ')' |
          ident + OneOrMore('[' + expression + ']') |
          ident | number | quotedString
          ) |
         ('(' + expression + ')')) +
        ZeroOrMore(uni_right_operator)
        )

    atom = cast_atom | uncast_atom

    expression << Group(atom + ZeroOrMore(bi_operator + atom))

    arrayOp = lbrack + expression + rbrack

    def recombine(tok):
        """Flattens a tree of tokens and joins into one big string.

        """
        return " ".join(flatten(tok.asList()))

    expression.setParseAction(recombine)

    def flatten(lst):
        res = []
        for i in lst:
            if isinstance(i, (list, tuple)):
                res.extend(flatten(i))
            else:
                res.append(str(i))
        return res

    def print_parse_results(pr, depth=0, name=''):
        """For debugging; pretty-prints parse result objects."""
        start = name + " " * (20 - len(name)) + ':' + '..' * depth
        if isinstance(pr, ParseResults):
            print(start)
            for i in pr:
                name = ''
                for k in pr.keys():
                    if pr[k] is i:
                        name = k
                        break
                print_parse_results(i, depth+1, name)
        else:
            print(start + str(pr))
