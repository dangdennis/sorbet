# typed: true
# M0 fixture. Each method's comment says what the measurement should report for it.

class Tax
  extend T::Sig

  # known: only calls typed stdlib methods
  sig { params(amount: Integer).returns(Integer) }
  def rate_for(amount)
    amount * 2 + 1
  end
end

class Invoice
  extend T::Sig

  sig { params(tax: Tax).void }
  def initialize(tax)
    @tax = T.let(tax, Tax)
    @items = T.let([], T::Array[Integer])
  end

  # known: every call resolves, including the block body
  sig { returns(Integer) }
  def total
    @items.sum { |i| i + @tax.rate_for(i) }
  end

  # unknown (local): untyped receiver
  sig { params(thing: T.untyped).returns(T.untyped) }
  def poke(thing)
    thing.frobnicate
  end

  # unknown (local): dynamic dispatch through send
  sig { params(name: Symbol).returns(T.untyped) }
  def dynamic(name)
    send(name)
  end

  # unknown (transitive): calls poke
  sig { params(thing: T.untyped).void }
  def indirect(thing)
    poke(thing)
  end

  # unknown (transitive): two hops away from poke
  sig { void }
  def two_hops
    indirect(nil)
  end

  # known: constructs an object, so it calls Tax#initialize implicitly
  sig { returns(Tax) }
  def self.default_tax
    Tax.new
  end

  # unknown (local): no signature, so the parameter is untyped
  def unsigged(other)
    other.total
  end
end
