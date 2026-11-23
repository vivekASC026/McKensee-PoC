"""The source accelerator: day 0 at a new customer.

The team at a new customer reads hundreds of tables by hand before anything
else can start. The accelerator does the reading -- structure, keys nobody
declared, a profile of the tables that matter -- and leaves the person the
decisions: what the records are, what they are called, which levels matter.

It only ever reads a source. Every statement goes through source_read, which
refuses anything that is not a SELECT, and what it learns is written under
work/accelerator/<source>/, never into the store or a pack.

Words used throughout: a *source* is a database being read; a *record kind* is
what a row of the main table stands for, the *primary* one and its *child*; a
child's *lines* are its rows one level down; a *level* groups records, with a
*label* to name each group; a *path* is the joins from a record to a level,
sometimes through a *bridge* table. No business words: those come from the
person, and belong in the pack the accelerator writes.
"""
