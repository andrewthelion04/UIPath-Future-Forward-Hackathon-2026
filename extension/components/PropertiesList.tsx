import type { Property } from '@/lib/api';

export default function PropertiesList({ properties }: { properties: Property[] }) {
  if (properties.length === 0) {
    return <p className="empty">No properties yet. Seed the world or sync your PMS.</p>;
  }
  return (
    <>
      {properties.map((property) => (
        <article key={property.id} className="card">
          <h2>{property.name}</h2>
          <p className="meta">
            {property.city} · {property.bedrooms} bedroom(s) · sleeps {property.max_guests} ·{' '}
            {property.base_price} {property.currency}/night
          </p>
          <p className="summary">{property.address}</p>
          <p className="tags">
            {property.amenities.map((amenity) => (
              <span key={amenity} className="tag">
                {amenity.replaceAll('_', ' ')}
              </span>
            ))}
          </p>
          <p className="meta">PMS connection: {property.pms_provider}</p>
        </article>
      ))}
    </>
  );
}
